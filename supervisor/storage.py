"""持久化存储（M1）：原子写、状态读取、目录布局。

- 所有 JSON 写入走 `tmp + flush + fsync + os.replace`（Unix rename 原子）。
- `.agent/state.json` 由 Parent 写，Supervisor 只读并严格校验。
- `.supervisor/runtime.json` 只允许 Supervisor 写。
"""

import json
import os
from pathlib import Path
from typing import Any, Dict, Optional

from .models import AgentState, AgentStateError, RuntimeState

_RUN_DIR_PREFIX = "activation-"


def atomic_write_json(path, data) -> None:
    """原子写 JSON（先写 tmp，fsync 后 os.replace）。"""
    path = Path(path)
    temp = path.with_suffix(path.suffix + ".tmp")
    with temp.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(temp, path)


def write_text_atomic(path, text) -> None:
    path = Path(path)
    temp = path.with_suffix(path.suffix + ".tmp")
    with temp.open("w", encoding="utf-8") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(temp, path)


def read_json_strict(path) -> Dict[str, Any]:
    """读取 JSON；语法错误给出带路径的明确 ValueError。"""
    path = Path(path)
    with path.open("r", encoding="utf-8") as f:
        try:
            data = json.load(f)
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid JSON in {path}: {exc}")
    if not isinstance(data, dict):
        raise ValueError(f"JSON in {path} is not an object")
    return data


def load_agent_state(path) -> Optional[AgentState]:
    """读取并校验 `.agent/state.json`。缺失 → None；非法 → AgentStateError。"""
    path = Path(path)
    if not path.exists():
        return None
    try:
        raw = read_json_strict(path)
    except ValueError as exc:
        raise AgentStateError(str(exc))
    try:
        return AgentState.from_dict(raw)
    except AgentStateError:
        raise
    except (KeyError, TypeError, ValueError) as exc:
        raise AgentStateError(f"invalid agent state in {path}: {exc}")


class Layout:
    """仓库内固定目录/文件布局（Supervisor 世界与 Parent 世界分离）。"""

    def __init__(self, base):
        self.base = Path(base)

    # Parent 世界（只读）
    @property
    def agent_state_path(self) -> Path:
        return self.base / ".agent" / "state.json"

    @property
    def agent_plan_path(self) -> Path:
        return self.base / ".agent" / "PLAN.md"

    # Supervisor 世界（只写）
    @property
    def supervisor_dir(self) -> Path:
        return self.base / ".supervisor"

    @property
    def runtime_path(self) -> Path:
        return self.supervisor_dir / "runtime.json"

    @property
    def events_path(self) -> Path:
        return self.supervisor_dir / "events.jsonl"

    @property
    def lock_path(self) -> Path:
        return self.supervisor_dir / "lock"

    @property
    def resume_path(self) -> Path:
        return self.supervisor_dir / "resume.json"

    @property
    def runs_dir(self) -> Path:
        return self.supervisor_dir / "runs"

    @property
    def inbox_dir(self) -> Path:
        return self.supervisor_dir / "inbox"

    def run_dir(self, activation_id: int) -> Path:
        return self.runs_dir / f"{_RUN_DIR_PREFIX}{activation_id:06d}"

    def ensure_dirs(self) -> None:
        self.supervisor_dir.mkdir(parents=True, exist_ok=True)
        self.runs_dir.mkdir(parents=True, exist_ok=True)
        self.inbox_dir.mkdir(parents=True, exist_ok=True)
        self.agent_state_path.parent.mkdir(parents=True, exist_ok=True)


class RuntimeStore:
    """`.supervisor/runtime.json` 的读写（永远原子写）。"""

    def __init__(self, layout: Layout):
        self.layout = layout

    def load(self) -> Optional[RuntimeState]:
        path = self.layout.runtime_path
        if not path.exists():
            return None
        try:
            raw = read_json_strict(path)
        except ValueError as exc:
            raise ValueError(f"corrupt runtime.json: {exc}")
        return RuntimeState.from_dict(raw)

    def save(self, state: RuntimeState) -> None:
        atomic_write_json(self.layout.runtime_path, state.to_dict())