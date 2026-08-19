"""DSH exec launcher（M5 崩溃恢复的关键工件）。

Supervisor 先在 runtime 里持久化 STARTING_PARENT + activation_token，
再启动本脚本。本脚本在 exec DSH **之前**，自己原子写进程记录
`.supervisor/runs/activation-N/process.json`（pid / start_id / token /
继承的租约 FD），然后 `os.execvp` 替换为 DSH（exec 前后同一 pid、同一 starttime）。

职责分工（P0-1 hardening）：
- `process.json` = **身份发现**：重启后的 Supervisor 据此知道旧激活的 pid/starttime/token。
- `parent.lock` 租约 = **唯一性保证**：Supervisor 在 spawn 前已 flock 该文件，
  并把已锁 FD 经 `pass_fds`/`SUPERVISOR_PARENT_LOCK_FD` 继承给本脚本，
  `os.execvp` 不清除该 FD → 由 exec 后的 DSH 继续持有。只要 DSH 活着，
  任何重启的 Supervisor 都拿不到租约，从而**绝不 spawn 第二个 Parent**。
"""

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from supervisor.process_identity import read_start_id  # noqa: E402


def _atomic_write_json(path, data) -> None:
    path = Path(path)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="supervisor-launcher")
    parser.add_argument("--record", required=True, help="process.json 路径")
    parser.add_argument("--token", required=True, help="activation token")
    parser.add_argument("--activation-id", type=int, required=True)
    parser.add_argument("cmd", nargs=argparse.REMAINDER, help="要 exec 的命令")
    args = parser.parse_args(argv)

    cmd = args.cmd
    if not cmd:
        print("launcher: no command to exec", file=sys.stderr)
        return 127

    # 继承的租约 FD（Supervisor 已 flock；exec DSH 后继续持有 = Parent 唯一性保证）
    lock_fd = None
    raw_fd = os.environ.get("SUPERVISOR_PARENT_LOCK_FD")
    if raw_fd:
        try:
            os.fstat(int(raw_fd))
            lock_fd = int(raw_fd)
        except (ValueError, OSError):
            lock_fd = None

    _atomic_write_json(
        args.record,
        {
            "pid": os.getpid(),
            "process_start_id": read_start_id(os.getpid()),
            "activation_id": args.activation_id,
            "activation_token": args.token,
            "parent_lock_fd": lock_fd,
            "written_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        },
    )

    try:
        os.execvp(cmd[0], cmd)
    except OSError as exc:
        print(f"launcher: cannot exec {cmd[0]}: {exc}", file=sys.stderr)
        return 127
    return 0  # unreachable


if __name__ == "__main__":
    sys.exit(main())