"""DSH Parent 进程运行器（M2+M5 hardening — 通过 launcher 自写 process.json 消除崩溃窗口）。

- 启动：python supervisor/launcher.py --record ... --token ... --activation-id ... dsh --profile headless "<prompt>"
  launcher 在 exec DSH 前原子写 .supervisor/runs/activation-N/process.json（pid/start_id/token）。
  即使 Supervisor 在 spawn 与 on_start 之间被 kill -9，子进程身份仍在磁盘上。
- 为进程创建独立 session/process group（start_new_session=True）
- Parent lease（P0-1）：Supervisor 已 flock 的 `.supervisor/parent.lock` FD 通过
  pass_fds 传给 launcher → exec 后由 DSH 继承持有；唯一性由租约保证，process.json 只做身份发现。
- 超时：SIGTERM 进程组 → grace → SIGKILL 进程组，并**确认整个 PGID 消失**
  （P0-2：只看 leader PID 会漏掉忽略 SIGTERM 的子进程）。
- 产出 ParentResult；127+stderr 标记视为 RunnerError（清晰报错）
"""

import asyncio
import os
import signal
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .models import ParentResult
from .process_identity import process_group_alive, read_start_id

_LAUNCHER = Path(__file__).resolve().parent / "launcher.py"


class RunnerError(Exception):
    """Failed to spawn or supervise the DSH process."""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _kill_group(pid: int, sig: signal.Signals) -> None:
    try:
        os.killpg(pid, sig)
    except (ProcessLookupError, PermissionError):
        pass


async def terminate_process_group(pid: int, grace_seconds: float) -> bool:
    """通用进程组终止（供 engine 在收养/收尾等路径复用）：

    SIGTERM 整组 → 轮询到 **整个 PGID 消失**（`process_group_alive` 判据，
    僵尸不算活着）→ 宽限后仍有成员则 SIGKILL 整组 → 确认 PGID 消失。
    绝不只检查 leader PID（leader 死了但子进程忽略 SIGTERM 也必须被清）。

    返回：True = 整个 PGID 已确认消失；False = SIGKILL+确认窗口后仍存活
    （调用方**必须**失败处理，不得继续宣称 PARENT_KILLED / STOPPED_OPERATOR）。
    """
    import time as _time
    import asyncio as _asyncio

    if not pid:
        return True
    _kill_group(pid, signal.SIGTERM)
    deadline = _time.monotonic() + max(0, grace_seconds)
    while _time.monotonic() < deadline:
        if not process_group_alive(pid):
            return True
        await _asyncio.sleep(0.05)
    if not process_group_alive(pid):
        return True
    # SIGKILL（极端情况如 D-state 一次不够，重试几轮，每轮给出回收窗口）
    for _ in range(3):
        _kill_group(pid, signal.SIGKILL)
        confirm = _time.monotonic() + 2.0
        while _time.monotonic() < confirm:
            if not process_group_alive(pid):
                return True
            await _asyncio.sleep(0.05)
    return False


class DshRunner:
    def __init__(self, executable="dsh", profile="headless", terminate_grace_seconds=10):
        self.executable = executable
        self.profile = profile
        self.terminate_grace_seconds = terminate_grace_seconds
        self.last_pid = None  # 审计/测试参考：最近一次 spawn 的 pid（引擎判定已统一经 STOPPING 收尾 reconciliation）

    async def run(
        self,
        *,
        repo,
        prompt: str,
        activation_id: int,
        timeout_seconds: int,
        run_dir,
        on_start=None,
        activation_token=None,
        lease_fd=None,
    ) -> ParentResult:
        repo = Path(repo)
        run_dir = Path(run_dir)
        run_dir.mkdir(parents=True, exist_ok=True)
        stdout_path = run_dir / "stdout.log"
        stderr_path = run_dir / "stderr.log"
        record_path = run_dir / "process.json"

        started_at = _now_iso()
        start_mono = __import__("time").monotonic()
        token = activation_token or uuid.uuid4().hex

        try:
            out_f = stdout_path.open("w", encoding="utf-8")
            err_f = stderr_path.open("w", encoding="utf-8")
        except OSError as exc:
            raise RunnerError(f"cannot open run logs in {run_dir}: {exc}")

        env = dict(os.environ)
        env["SUPERVISOR_ACTIVATION_ID"] = str(activation_id)
        env["SUPERVISOR_ACTIVATION_TOKEN"] = token
        if lease_fd is not None:
            env["SUPERVISOR_PARENT_LOCK_FD"] = str(lease_fd)
        cmd = [
            sys.executable,
            str(_LAUNCHER),
            "--record",
            str(record_path),
            "--token",
            token,
            "--activation-id",
            str(activation_id),
            self.executable,
            "--profile",
            self.profile,
            prompt,
        ]
        env["SUPERVISOR_RUN_DIR"] = str(run_dir)
        spawn_kwargs = dict(
            cwd=str(repo),
            stdout=out_f,
            stderr=err_f,
            start_new_session=True,
            env=env,
        )
        if lease_fd is not None:
            # 把已 flock 的租约 FD 交给 launcher（exec DSH 后继续持有 = 唯一性保证）
            spawn_kwargs["pass_fds"] = (lease_fd,)
        try:
            proc = await asyncio.create_subprocess_exec(*cmd, **spawn_kwargs)
        except FileNotFoundError as exc:
            out_f.close()
            err_f.close()
            raise RunnerError(f"cannot exec launcher {sys.executable!r}: {exc}")
        except OSError as exc:
            out_f.close()
            err_f.close()
            raise RunnerError(f"failed to spawn launcher: {exc}")

        pid = proc.pid
        self.last_pid = pid
        process_start_id = read_start_id(pid)
        if on_start is not None:
            on_start(pid, process_start_id)

        timed_out = False
        group_survived = False
        try:
            await asyncio.wait_for(proc.wait(), timeout=timeout_seconds)
        except asyncio.TimeoutError:
            timed_out = True
            group_survived = not await self._terminate_group(proc)
        except asyncio.CancelledError:
            await self._terminate_group(proc)
            raise

        ended_at = _now_iso()
        duration = __import__("time").monotonic() - start_mono
        exit_code = proc.returncode
        # launcher exec 失败：通过 127 + stderr 标记转为 RunnerError
        if exit_code == 127:
            try:
                stderr_text = stderr_path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                stderr_text = ""
            if "launcher: cannot exec" in stderr_text:
                raise RunnerError(stderr_text.strip().splitlines()[-1])

        return ParentResult(
            activation_id=activation_id,
            exit_code=exit_code,
            timed_out=timed_out,
            started_at=started_at,
            ended_at=ended_at,
            duration_seconds=round(duration, 3),
            stdout_path=str(stdout_path),
            stderr_path=str(stderr_path),
            run_dir=str(run_dir),
            reason="",
            pid=pid,
            process_start_id=process_start_id,
            group_survived=group_survived,
        )

    async def _terminate_group(self, proc) -> bool:
        """SIGTERM 整组 → 等 leader 回收/宽限 → 还有成员则 SIGKILL → 确认 PGID 消失。

        返回 True = 整个 PGID 已确认消失；False = 仍在（调用方必须失败处理）。
        """
        import time as _time

        pgid = proc.pid
        grace_dl = _time.monotonic() + max(0, self.terminate_grace_seconds)
        _kill_group(pgid, signal.SIGTERM)
        try:
            await asyncio.wait_for(proc.wait(), timeout=self.terminate_grace_seconds)
        except asyncio.TimeoutError:
            # leader 不配合（忽略 SIGTERM）：grace 一到立即 escalate
            _kill_group(pgid, signal.SIGKILL)
            try:
                await asyncio.wait_for(proc.wait(), timeout=2.0)
            except asyncio.TimeoutError:
                pass
        if not process_group_alive(pgid):
            return True  # 整组已清（快路径）
        # leader 已退出但组内还有成员（如忽略 SIGTERM 的子进程）：把剩余 grace 给它们，
        # 到点仍不退出就 SIGKILL，并最终确认整个 PGID 消失
        while _time.monotonic() < grace_dl and process_group_alive(pgid):
            await asyncio.sleep(0.05)
        if not process_group_alive(pgid):
            return True
        for _ in range(3):
            _kill_group(pgid, signal.SIGKILL)
            dl2 = _time.monotonic() + 2.0
            while _time.monotonic() < dl2:
                if not process_group_alive(pgid):
                    return True
                await asyncio.sleep(0.05)
        return False

    @staticmethod
    def _kill_group(pid: int, sig: signal.Signals) -> None:
        _kill_group(pid, sig)