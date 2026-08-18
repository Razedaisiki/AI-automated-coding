"""数据模型与协议枚举（M1）。

两个状态文件的所有权规则见 docs/supervisor-protocol.md：

- `.agent/state.json`      Parent 写、Supervisor 只读 —— 开发任务状态
- `.supervisor/runtime.json` Supervisor 写 —— 自动化运行状态
"""

from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, Optional


class AgentStateError(ValueError):
    """Invalid or unreadable .agent/state.json."""


class AgentStatus(str, Enum):
    """Parent 可以写入的五个 status（唯一枚举）。"""

    RUNNING = "RUNNING"
    WAIT_CI = "WAIT_CI"
    WAIT_HUMAN = "WAIT_HUMAN"
    COMPLETED = "COMPLETED"
    BLOCKED = "BLOCKED"


class SupervisorStatus(str, Enum):
    """Supervisor 自己的状态机（无开发语义）。"""

    BOOTING = "BOOTING"
    STARTING_PARENT = "STARTING_PARENT"
    RUNNING_PARENT = "RUNNING_PARENT"
    RESTART_BACKOFF = "RESTART_BACKOFF"
    WAITING_CI = "WAITING_CI"
    WAITING_HUMAN = "WAITING_HUMAN"
    STOPPING = "STOPPING"
    STOPPED_SUCCESS = "STOPPED_SUCCESS"
    STOPPED_BLOCKED = "STOPPED_BLOCKED"
    STOPPED_LIMIT = "STOPPED_LIMIT"
    STOPPED_ERROR = "STOPPED_ERROR"
    STOPPED_OPERATOR = "STOPPED_OPERATOR"


class CiStatus(str, Enum):
    """CI provider 统一返回。"""

    NONE = "NONE"
    NOT_FOUND = "NOT_FOUND"
    PENDING = "PENDING"
    SUCCESS = "SUCCESS"
    FAILURE = "FAILURE"
    CANCELLED = "CANCELLED"
    ERROR = "ERROR"
    TIMEOUT = "TIMEOUT"


class StopReason(str, Enum):
    """固定枚举，禁止保存 "something went wrong"。"""

    TASK_COMPLETED = "TASK_COMPLETED"
    TASK_BLOCKED = "TASK_BLOCKED"
    MAX_PARENT_ACTIVATIONS = "MAX_PARENT_ACTIVATIONS"
    MAX_CRASH_RESTARTS = "MAX_CRASH_RESTARTS"
    MAX_CLEAN_RESTARTS = "MAX_CLEAN_RESTARTS"
    MAX_TIMEOUTS = "MAX_TIMEOUTS"
    MAX_ACTIVE_WALL_TIME = "MAX_ACTIVE_WALL_TIME"
    CI_WAIT_TIMEOUT = "CI_WAIT_TIMEOUT"
    INVALID_AGENT_STATE = "INVALID_AGENT_STATE"
    SUPERVISOR_INTERNAL_ERROR = "SUPERVISOR_INTERNAL_ERROR"
    OPERATOR_STOP = "OPERATOR_STOP"


AGENT_SCHEMA_VERSION = 1
RUNTIME_SCHEMA_VERSION = 1


@dataclass
class AgentState:
    """`.agent/state.json` —— Parent 写的开发状态，Supervisor 只读。"""

    schema_version: int
    status: AgentStatus
    checkpoint_seq: int
    updated_at: str
    task_id: Optional[str] = None
    checkpoint: Optional[Dict[str, Any]] = None
    git: Optional[Dict[str, Any]] = None
    ci: Optional[Dict[str, Any]] = None
    next_action: Optional[str] = None
    blocker: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "task_id": self.task_id,
            "status": self.status.value,
            "checkpoint_seq": self.checkpoint_seq,
            "checkpoint": self.checkpoint,
            "git": self.git,
            "ci": self.ci,
            "next_action": self.next_action,
            "blocker": self.blocker,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, raw: Dict[str, Any]) -> "AgentState":
        if not isinstance(raw, dict):
            raise AgentStateError("agent state must be a JSON object")
        try:
            schema_version = raw["schema_version"]
            status_raw = raw["status"]
            checkpoint_seq = raw["checkpoint_seq"]
            updated_at = raw["updated_at"]
        except KeyError as exc:
            raise AgentStateError(f"agent state missing required field: {exc.args[0]}")

        if not isinstance(schema_version, int) or schema_version != AGENT_SCHEMA_VERSION:
            raise AgentStateError(
                f"unsupported agent state schema_version={schema_version!r} "
                f"(expected {AGENT_SCHEMA_VERSION})"
            )
        if not isinstance(status_raw, str) or status_raw not in AgentStatus._value2member_map_:
            raise AgentStateError(f"invalid agent status: {status_raw!r}")
        if not isinstance(checkpoint_seq, int) or isinstance(checkpoint_seq, bool) or checkpoint_seq < 0:
            raise AgentStateError(f"checkpoint_seq must be a non-negative int, got {checkpoint_seq!r}")
        if not isinstance(updated_at, str) or not updated_at:
            raise AgentStateError("agent state missing valid updated_at")

        return cls(
            schema_version=schema_version,
            status=AgentStatus(status_raw),
            checkpoint_seq=checkpoint_seq,
            updated_at=updated_at,
            task_id=raw.get("task_id"),
            checkpoint=raw.get("checkpoint"),
            git=raw.get("git"),
            ci=raw.get("ci"),
            next_action=raw.get("next_action"),
            blocker=raw.get("blocker"),
        )


def agent_state_from_dict(raw: Dict[str, Any]) -> AgentState:
    return AgentState.from_dict(raw)


@dataclass
class ParentInfo:
    """当前激活的 Parent 进程记录（用于崩溃后进程身份校验）。"""

    activation_id: int
    pid: Optional[int]
    process_start_id: Optional[str]
    started_at: Optional[str]
    reason: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "activation_id": self.activation_id,
            "pid": self.pid,
            "process_start_id": self.process_start_id,
            "started_at": self.started_at,
            "reason": self.reason,
        }

    @classmethod
    def from_dict(cls, raw: Dict[str, Any]) -> "ParentInfo":
        return cls(
            activation_id=int(raw["activation_id"]),
            pid=raw.get("pid"),
            process_start_id=raw.get("process_start_id"),
            started_at=raw.get("started_at"),
            reason=raw.get("reason", ""),
        )


@dataclass
class Counters:
    parent_activations: int = 0
    crash_restarts: int = 0
    clean_restarts: int = 0
    timeouts: int = 0
    ci_wakeups: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "parent_activations": self.parent_activations,
            "crash_restarts": self.crash_restarts,
            "clean_restarts": self.clean_restarts,
            "timeouts": self.timeouts,
            "ci_wakeups": self.ci_wakeups,
        }

    @classmethod
    def from_dict(cls, raw: Dict[str, Any]) -> "Counters":
        return cls(
            parent_activations=int(raw.get("parent_activations", 0)),
            crash_restarts=int(raw.get("crash_restarts", 0)),
            clean_restarts=int(raw.get("clean_restarts", 0)),
            timeouts=int(raw.get("timeouts", 0)),
            ci_wakeups=int(raw.get("ci_wakeups", 0)),
        )


@dataclass
class Limits:
    max_parent_activations: int
    max_crash_restarts: int
    max_clean_restarts: int
    max_timeouts: int
    max_ci_wakeups: int
    max_active_wall_seconds: int
    parent_timeout_seconds: int
    terminate_grace_seconds: int

    def to_dict(self) -> Dict[str, Any]:
        return {
            "max_parent_activations": self.max_parent_activations,
            "max_crash_restarts": self.max_crash_restarts,
            "max_clean_restarts": self.max_clean_restarts,
            "max_timeouts": self.max_timeouts,
            "max_ci_wakeups": self.max_ci_wakeups,
            "max_active_wall_seconds": self.max_active_wall_seconds,
            "parent_timeout_seconds": self.parent_timeout_seconds,
            "terminate_grace_seconds": self.terminate_grace_seconds,
        }

    @classmethod
    def from_dict(cls, raw: Dict[str, Any]) -> "Limits":
        return cls(
            max_parent_activations=int(raw["max_parent_activations"]),
            max_crash_restarts=int(raw["max_crash_restarts"]),
            max_clean_restarts=int(raw["max_clean_restarts"]),
            max_timeouts=int(raw["max_timeouts"]),
            max_ci_wakeups=int(raw["max_ci_wakeups"]),
            max_active_wall_seconds=int(raw["max_active_wall_seconds"]),
            parent_timeout_seconds=int(raw["parent_timeout_seconds"]),
            terminate_grace_seconds=int(raw["terminate_grace_seconds"]),
        )


@dataclass
class RuntimeState:
    """`.supervisor/runtime.json` —— 自动化运行状态，只允许 Supervisor 写。"""

    schema_version: int
    status: SupervisorStatus
    task_started_at: str
    current_parent: Optional[ParentInfo]
    counters: Counters
    limits: Limits
    last_agent_checkpoint_seq: int
    supervisor_pid: Optional[int]
    active_budget: Optional[Dict[str, Any]]
    stop_reason: Optional[StopReason]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "status": self.status.value,
            "task_started_at": self.task_started_at,
            "current_parent": self.current_parent.to_dict() if self.current_parent else None,
            "counters": self.counters.to_dict() if self.counters else None,
            "limits": self.limits.to_dict() if self.limits else None,
            "last_agent_checkpoint_seq": self.last_agent_checkpoint_seq,
            "supervisor_pid": self.supervisor_pid,
            "active_budget": self.active_budget,
            "stop_reason": self.stop_reason.value if self.stop_reason else None,
        }

    @classmethod
    def from_dict(cls, raw: Dict[str, Any]) -> "RuntimeState":
        schema_version = raw.get("schema_version")
        if not isinstance(schema_version, int) or schema_version != RUNTIME_SCHEMA_VERSION:
            raise ValueError(
                f"unsupported runtime schema_version={schema_version!r} (expected {RUNTIME_SCHEMA_VERSION})"
            )
        status_raw = raw.get("status")
        if status_raw not in SupervisorStatus._value2member_map_:
            raise ValueError(f"invalid supervisor status: {status_raw!r}")
        counters_raw = raw.get("counters")
        limits_raw = raw.get("limits")
        parent_raw = raw.get("current_parent")
        return cls(
            schema_version=schema_version,
            status=SupervisorStatus(status_raw),
            task_started_at=raw.get("task_started_at", ""),
            current_parent=ParentInfo.from_dict(parent_raw) if parent_raw else None,
            counters=Counters.from_dict(counters_raw) if counters_raw else None,
            limits=Limits.from_dict(limits_raw) if limits_raw else None,
            last_agent_checkpoint_seq=int(raw.get("last_agent_checkpoint_seq", 0)),
            supervisor_pid=raw.get("supervisor_pid"),
            active_budget=raw.get("active_budget"),
            stop_reason=StopReason(raw["stop_reason"]) if raw.get("stop_reason") else None,
        )


def runtime_state_from_dict(raw: Dict[str, Any]) -> RuntimeState:
    return RuntimeState.from_dict(raw)


@dataclass
class ParentResult:
    """一次 DSH Parent activation 的结果（DshRunner 产出）。"""

    activation_id: int
    exit_code: Optional[int]
    timed_out: bool
    started_at: str
    ended_at: str
    duration_seconds: float
    stdout_path: str
    stderr_path: str
    run_dir: str
    reason: str = ""
    pid: Optional[int] = None
    process_start_id: Optional[str] = None


@dataclass
class GitSnapshot:
    """Git 机械状态快照（Supervisor 只看结构，不看 diff 内容）。"""

    branch: Optional[str] = None
    head: Optional[str] = None
    dirty: bool = False
    has_remote: bool = False
    remote_url: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "branch": self.branch,
            "head": self.head,
            "dirty": self.dirty,
            "has_remote": self.has_remote,
            "remote_url": self.remote_url,
        }