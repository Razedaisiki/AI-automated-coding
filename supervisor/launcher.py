"""DSH exec launcher（M5 崩溃恢复的关键工件）。

Supervisor 先在 runtime 里持久化 STARTING_PARENT + activation_token，
再启动本脚本。本脚本在 exec DSH **之前**，自己原子写进程记录
`.supervisor/runs/activation-N/process.json`（pid / start_id / token），
然后 `os.execvp` 替换为 DSH（exec 前后同一 pid、同一 starttime）。

即使 Supervisor 在 spawn 与 on_start（pid 落盘）之间被 kill -9，
子进程的身份信息也已经由它自己留在磁盘上 —— 重启后的 Supervisor
据此决定收养还是重新 spawn，彻底杜绝重复 Parent。
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

    _atomic_write_json(
        args.record,
        {
            "pid": os.getpid(),
            "process_start_id": read_start_id(os.getpid()),
            "activation_id": args.activation_id,
            "activation_token": args.token,
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