"""DSH Parent 进程运行器（M2）。

只做进程管理，不做任何开发决策：

- 在 repo 目录中启动 `dsh --profile headless "<prompt>"`（exec 数组，无 shell）
- 为进程创建独立 session/process group（start_new_session=True）
- stdout/stderr 落到运行目录
- 超时：SIGTERM 整个进程组 → grace → SIGKILL 整个进程组
- 产出 ParentResult（含进程身份，供 M5 崩溃恢复使用）
"""

import asyncio
import os
import signal
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .models import ParentResult
from .process_identity import read_start_id


class RunnerError(Exception):
    """Failed to spawn or supervise the DSH process."""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


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
    ) -> ParentResult:
        repo = Path(repo)
        run_dir = Path(run_dir)
        run_dir.mkdir(parents=True, exist_ok=True)
        stdout_path = run_dir / "stdout.log"
        stderr_path = run_dir / "stderr.log"

        started_at = _now_iso()
        start_mono = __import__("time").monotonic()

        try:
            out_f = stdout_path.open("w", encoding="utf-8")
            err_f = stderr_path.open("w", encoding="utf-8")
        except OSError as exc:
            raise RunnerError(f"cannot open run logs in {run_dir}: {exc}")

        try:
            proc = await asyncio.create_subprocess_exec(
                self.executable,
                "--profile",
                self.profile,
                prompt,
                cwd=str(repo),
                stdout=out_f,
                stderr=err_f,
                start_new_session=True,
            )
        except FileNotFoundError as exc:
            out_f.close()
            err_f.close()
            raise RunnerError(
                f"cannot exec DSH executable {self.executable!r}: {exc}"
            )
        except OSError as exc:
            out_f.close()
            err_f.close()
            raise RunnerError(f"failed to spawn DSH process: {exc}")

        pid = proc.pid
        process_start_id = read_start_id(pid)

        timed_out = False
        try:
            await asyncio.wait_for(proc.wait(), timeout=timeout_seconds)
        except asyncio.TimeoutError:
            timed_out = True
            await self._terminate_group(proc)

        ended_at = _now_iso()
        duration = __import__("time").monotonic() - start_mono

        return ParentResult(
            activation_id=activation_id,
            exit_code=proc.returncode,
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
        """SIGTERM 进程组 → grace → SIGKILL 进程组。"""
        self._kill_group(proc.pid, signal.SIGTERM)
        try:
            await asyncio.wait_for(proc.wait(), timeout=self.terminate_grace_seconds)
        except asyncio.TimeoutError:
            self._kill_group(proc.pid, signal.SIGKILL)
            await proc.wait()

    @staticmethod
    def _kill_group(pid: int, sig: signal.Signals) -> None:
        try:
            os.killpg(pid, sig)
        except (ProcessLookupError, PermissionError):
            pass  # 进程组已消失