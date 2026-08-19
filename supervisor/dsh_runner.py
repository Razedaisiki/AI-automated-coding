"""DSH Parent 进程运行器（M2+M5 hardening — 通过 launcher 自写 process.json 消除崩溃窗口）。

- 启动：python supervisor/launcher.py --record ... --token ... --activation-id ... dsh --profile headless "<prompt>"
  launcher 在 exec DSH 前原子写 .supervisor/runs/activation-N/process.json（pid/start_id/token）。
  即使 Supervisor 在 spawn 与 on_start 之间被 kill -9，子进程身份仍在磁盘上。
- 为进程创建独立 session/process group（start_new_session=True）
- 超时：SIGTERM 进程组 → grace → SIGKILL 进程组
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
from .process_identity import read_start_id

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


async def terminate_process_group(pid: int, grace_seconds: float) -> None:
    """通用进程组终止（供 engine 在收养等路径复用）：SIGTERM→轮询 grace→SIGKILL。"""
    from .process_identity import is_proc_alive

    _kill_group(pid, signal.SIGTERM)
    deadline = __import__("time").monotonic() + max(0, grace_seconds)
    while __import__("time").monotonic() < deadline:
        if not is_proc_alive(pid):
            return
        await __import__("asyncio").sleep(0.05)
    if not is_proc_alive(pid):
        return
    _kill_group(pid, signal.SIGKILL)
    # SIGKILL 后再给一点时间让内核回收
    deadline = __import__("time").monotonic() + 1.0
    while __import__("time").monotonic() < deadline:
        if not is_proc_alive(pid):
            return
        await __import__("asyncio").sleep(0.05)


class DshRunner:
    def __init__(self, executable="dsh", profile="headless", terminate_grace_seconds=10):
        self.executable = executable
        self.profile = profile
        self.terminate_grace_seconds = terminate_grace_seconds

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
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=str(repo),
                stdout=out_f,
                stderr=err_f,
                start_new_session=True,
                env=env,
            )
        except FileNotFoundError as exc:
            out_f.close()
            err_f.close()
            raise RunnerError(f"cannot exec launcher {sys.executable!r}: {exc}")
        except OSError as exc:
            out_f.close()
            err_f.close()
            raise RunnerError(f"failed to spawn launcher: {exc}")

        pid = proc.pid
        process_start_id = read_start_id(pid)
        if on_start is not None:
            on_start(pid, process_start_id)

        timed_out = False
        try:
            await asyncio.wait_for(proc.wait(), timeout=timeout_seconds)
        except asyncio.TimeoutError:
            timed_out = True
            await self._terminate_group(proc)
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
        )

    async def _terminate_group(self, proc) -> None:
        _kill_group(proc.pid, signal.SIGTERM)
        try:
            await asyncio.wait_for(proc.wait(), timeout=self.terminate_grace_seconds)
        except asyncio.TimeoutError:
            _kill_group(proc.pid, signal.SIGKILL)
            await proc.wait()

    @staticmethod
    def _kill_group(pid: int, sig: signal.Signals) -> None:
        _kill_group(pid, sig)