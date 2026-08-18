"""只追加的事件日志（M1）。

`.supervisor/events.jsonl`：每行一个 JSON 对象，绝不覆盖。
grep PARENT_STARTED events.jsonl 即可回答"为何唤醒 N 次 Parent"。
"""

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

# 事件名常量（保持拼写稳定，供审计/测试引用）
SUPERVISOR_STARTED = "SUPERVISOR_STARTED"
SUPERVISOR_STOPPED = "SUPERVISOR_STOPPED"
SUPERVISOR_CRASH_RECOVERY = "SUPERVISOR_CRASH_RECOVERY"
PARENT_STARTED = "PARENT_STARTED"
PARENT_EXITED = "PARENT_EXITED"
PARENT_TIMEOUT = "PARENT_TIMEOUT"
PARENT_KILLED = "PARENT_KILLED"
PARENT_CRASH = "PARENT_CRASH"
PARENT_CLEAN_EXIT_WITH_RUNNING_STATE = "PARENT_CLEAN_EXIT_WITH_RUNNING_STATE"
PARENT_NO_PROGRESS = "PARENT_NO_PROGRESS"
AGENT_STATE = "AGENT_STATE"
AGENT_STATE_INVALID = "AGENT_STATE_INVALID"
RESTART_BACKOFF = "RESTART_BACKOFF"
LIMIT_REACHED = "LIMIT_REACHED"
LOCK_REJECTED = "LOCK_REJECTED"
LOCK_ACQUIRED = "LOCK_ACQUIRED"
ORPHAN_ADOPTED = "ORPHAN_ADOPTED"
ORPHAN_EXITED = "ORPHAN_EXITED"
CI_DISABLED = "CI_DISABLED"
WAIT_HUMAN = "WAIT_HUMAN"
WAIT_CI = "WAIT_CI"
OPERATOR_STOP = "OPERATOR_STOP"
RESUME_RECEIVED = "RESUME_RECEIVED"
AGENT_ANOMALY = "AGENT_ANOMALY"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class EventLog:
    def __init__(self, path):
        self.path = Path(path)

    def emit(self, event: str, **fields: Any) -> None:
        line = {"ts": now_iso(), "event": event}
        line.update(fields)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(line, ensure_ascii=False) + "\n")
            f.flush()

    def read_all(self) -> List[Dict[str, Any]]:
        if not self.path.exists():
            return []
        rows = []
        with self.path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    # 半写行（极端崩溃）不阻止审计
                    rows.append({"event": "CORRUPT_LINE", "raw": line[:200]})
        return rows

    def tail(self, n: int = 10) -> List[Dict[str, Any]]:
        return self.read_all()[-n:]

    def events_named(self, name: str) -> List[Dict[str, Any]]:
        return [r for r in self.read_all() if r.get("event") == name]