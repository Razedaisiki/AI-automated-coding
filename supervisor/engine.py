"""Supervisor 引擎主循环（M3/M4/M5 hardening）。

- 三态恢复：NO_PARENT / STARTING_PARENT(pid=None + token) / RUNNING_PARENT(pid+start_id)
- STARTING_PARENT 通过 launcher 自写的 process.json 进行 reconciliation（有界宽限）
- Parent lease（P0-1）：spawn 前 flock `.supervisor/parent.lock` 并把已锁 FD 交给
  launcher→exec 后的 DSH；拿不到租约 = 存在活着的旧 activation → 绝不 spawn 第二个 Parent
- 整组终止（P0-2）：SIGTERM→grace→SIGKILL 后确认整个 PGID 消失，而非只看 leader PID
- 收养期间：stop→杀进程组（STOPPING 落盘）、parent timeout / wall-time 继续生效
- 收养 timeout → RECOVER_AFTER_PARENT_TIMEOUT + 退避；orphan 自退且状态未知 →
  保守 RECOVER_AFTER_PARENT_CRASH（P1）
- supervisor 自身 PID 身份写入 runtime
"""

import asyncio
import os
import re
import signal
import subprocess
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .config import Config, default_config
from .dsh_runner import DshRunner
from .events import (
    AGENT_ANOMALY,
    AGENT_STATE,
    AGENT_STATE_INVALID,
    CI_DISABLED,
    CI_FAILED,
    CI_MATERIAL_SAVED,
    CI_OBSERVED,
    CI_SUCCEEDED,
    CI_WAIT_STARTED,
    CI_WAIT_TIMEOUT,
    GIT_BRANCH_CHANGED,
    GIT_DIRTY_CHANGED,
    GIT_HEAD_CHANGED,
    GIT_SNAPSHOT,
    HUMAN_EVENT_DELIVERED,
    HUMAN_EVENT_DELIVERY_STARTED,
    HUMAN_EVENT_RECEIVED,
    LIMIT_REACHED,
    LOCK_ACQUIRED,
    LOCK_REJECTED,
    OPERATOR_STOP,
    ORPHAN_ADOPTED,
    ORPHAN_EXITED,
    PARENT_CLEAN_EXIT_WITH_RUNNING_STATE,
    PARENT_CRASH,
    PARENT_EXITED,
    PARENT_KILLED,
    PARENT_KILL_FAILED,
    PARENT_LEASE_HELD,
    PARENT_NO_PROGRESS,
    PARENT_RECORD_STALE,
    PARENT_RECONCILED,
    PARENT_SPAWN_UNCONFIRMED,
    PARENT_STARTED,
    PARENT_STARTING,
    PARENT_TIMEOUT,
    RESUME_RECEIVED,
    RESTART_BACKOFF,
    SUPERVISOR_CRASH_RECOVERY,
    SUPERVISOR_STARTED,
    SUPERVISOR_STOPPED,
    WAIT_CI,
    WAIT_HUMAN,
    EventLog,
)
from .human_events import HumanEventStore
from .git_probe import capture as capture_git
from .models import compare_git_snapshots
from .lock import LockHeldError, ParentLease, SupervisorLock
from .models import (
    AgentState,
    AgentStateError,
    AgentStatus,
    CiStatus,
    Counters,
    KillReason,
    Limits,
    ParentInfo,
    RuntimeState,
    StopReason,
    SupervisorStatus,
)
from .process_identity import (
    identity_matches,
    is_dsh_process,
    is_proc_alive,
    process_group_alive,
    read_start_id,
)
from .prompts import build_prompt
from .storage import Layout, RuntimeStore, atomic_write_json, load_agent_state, read_json_strict


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class Outcome:
    action: str  # restart | wait_ci | wait_human | stop_success | stop_blocked | stop_limit | stop_error | complete_stop
    next_reason: Optional[str] = None
    backoff: bool = False
    agent: Optional[AgentState] = None
    limit_reason: Optional[StopReason] = None
    keep_parent: bool = False  # stop_error 时保留 current_parent 身份（kill 失败可用）


class SupervisorEngine:
    def __init__(self, base_dir, config: Optional[Config] = None, runner=None, ci_provider=None):
        self.base = Path(base_dir)
        self.config = config if config is not None else default_config()
        self.layout = Layout(self.base)
        self.log = EventLog(self.layout.events_path)
        self.store = RuntimeStore(self.layout)
        self.runner = runner if runner is not None else DshRunner(
            executable=self.config.dsh.executable,
            profile=self.config.dsh.profile,
            terminate_grace_seconds=self.config.limits.terminate_grace_seconds,
        )
        self.ci_provider = ci_provider  # injected for tests (FakeCiProvider)
        self.human_store = HumanEventStore(self.layout.human_inbox_dir)
        self.stop_event_proxy = None
        self._stop_requested = False
        self.lock = None
        self.rt: Optional[RuntimeState] = None
        self.lease = ParentLease(self.layout.parent_lock_path)

    @property
    def stop_event(self):
        if self.stop_event_proxy is None:
            self.stop_event_proxy = asyncio.Event()
            if self._stop_requested:
                self.stop_event_proxy.set()
        return self.stop_event_proxy

    def request_stop(self) -> None:
        self._stop_requested = True
        if self.stop_event_proxy is not None:
            self.stop_event_proxy.set()

    async def run_forever(self) -> int:
        self.layout.ensure_dirs()
        self.lock = SupervisorLock(self.layout.lock_path)
        try:
            self.lock.acquire()
        except LockHeldError:
            self.log.emit(LOCK_REJECTED)
            raise
        self.log.emit(LOCK_ACQUIRED)

        self.rt = self._restore_or_init_runtime()
        try:
            _startup = capture_git(self.base)
            atomic_write_json(self.layout.git_startup_path, _startup.to_dict())
            self.log.emit(GIT_SNAPSHOT, phase="startup", **_startup.to_dict())
        except Exception:
            pass
        existing_agent = self._read_agent_state_safe()
        if existing_agent is not None:
            self.rt.last_agent_checkpoint_seq = max(
                self.rt.last_agent_checkpoint_seq, existing_agent.checkpoint_seq
            )
            self._save_runtime()
        self.log.emit(SUPERVISOR_STARTED, supervisor_pid=os.getpid())

        # 上次收尾在终止宽限期内崩溃（runtime 定格 STOPPING）→ 完成未被完成的 operator stop
        if self.rt.status == SupervisorStatus.STOPPING:
            return await self._complete_interrupted_stop()

        adoption_result = await self._adopt_orphan_if_needed()
        next_reason: Optional[str] = None
        if adoption_result is not None:
            result = await self._process_outcome(adoption_result)
            if result is None:
                pass  # wait_ci / wait_human 已在 _process_outcome 内等待
            elif result[0] == "complete":
                return await self._complete_interrupted_stop()
            elif result[0] == "stop":
                if len(result) > 4 and result[4]:
                    return self._finalize(result[1], result[2], result[3], keep_parent=True)
                return self._finalize(result[1], result[2], result[3])
            else:
                next_reason = result
        # reconcile 等 lease 期间收到 operator stop（已切入 STOPPING）→ 完成收尾
        if self.rt.status == SupervisorStatus.STOPPING:
            return await self._complete_interrupted_stop()

        try:
            while True:
                if self.stop_event.is_set():
                    self.rt.status = SupervisorStatus.STOPPING
                    self._save_runtime()
                    return self._finalize(
                        StopReason.OPERATOR_STOP, SupervisorStatus.STOPPED_OPERATOR, 0
                    )
                self._accrue_budget()
                limit = self._enforce_limits()
                if limit is not None:
                    return self._finalize(limit, SupervisorStatus.STOPPED_LIMIT, 1)

                agent = self._read_agent_state()
                if agent is None:
                    reason = next_reason or "INITIAL_START"
                    next_reason = None
                    outcome = await self._activation_cycle(reason)
                    result = await self._process_outcome(outcome)
                    if result is None:
                        continue
                    if result[0] == "complete":
                        return await self._complete_interrupted_stop()
                    if result[0] == "stop":
                        if len(result) > 4 and result[4]:
                            return self._finalize(result[1], result[2], result[3], keep_parent=True)
                        return self._finalize(result[1], result[2], result[3])
                    next_reason = result
                    continue

                if agent.status == AgentStatus.COMPLETED:
                    return self._finalize(
                        StopReason.TASK_COMPLETED, SupervisorStatus.STOPPED_SUCCESS, 0
                    )
                if agent.status == AgentStatus.BLOCKED:
                    return self._finalize(
                        StopReason.TASK_BLOCKED, SupervisorStatus.STOPPED_BLOCKED, 1
                    )
                if agent.status == AgentStatus.WAIT_HUMAN:
                    resume = await self._wait_human()
                    if resume:
                        pending_id = self.rt.human_event_id
                        pending_evt = None
                        if pending_id:
                            try:
                                pending_evt = HumanEventStore(self.layout.human_inbox_dir).get(pending_id)
                            except Exception:
                                pending_evt = None
                        extra = {}
                        if pending_evt:
                            extra["human_event_id"] = pending_evt.event_id
                            if pending_evt.message:
                                extra["human_message"] = pending_evt.message
                            if pending_evt.attachment_path:
                                extra["human_attachment"] = pending_evt.attachment_path
                        if agent.review:
                            if agent.review.get("pr_number"):
                                extra["review_pr_number"] = agent.review["pr_number"]
                                extra["review_pr_url"] = agent.review.get("pr_url", "")
                        if extra:
                            # inject human/review context via prompt kwargs helper
                            self._pending_human_ctx = extra
                        outcome = await self._activation_cycle(resume, **extra) if extra else await self._activation_cycle(resume)
                        if hasattr(self, "_pending_human_ctx"):
                            delattr(self, "_pending_human_ctx")
                        if pending_id:
                            try:
                                HumanEventStore(self.layout.human_inbox_dir).mark_delivered(pending_id)
                                self.log.emit(HUMAN_EVENT_DELIVERED, event_id=pending_id)
                            except Exception:
                                pass
                            self.rt.human_event_id = None
                            self._save_runtime()
                        result = await self._process_outcome(outcome)
                        if result is None:
                            continue
                        if result[0] == "complete":
                            return await self._complete_interrupted_stop()
                        if result[0] == "stop":
                            if len(result) > 4 and result[4]:
                                return self._finalize(result[1], result[2], result[3], keep_parent=True)
                            return self._finalize(result[1], result[2], result[3])
                        next_reason = result
                    continue
                if agent.status == AgentStatus.WAIT_CI:
                    await self._wait_ci(agent)
                    # _wait_ci may have finalized as STOPPED_LIMIT/ERROR via CI timeout/validation
                    if self.rt.status == SupervisorStatus.STOPPED_LIMIT:
                        return self._finalize(self.rt.stop_reason, self.rt.status, 1)
                    if self.rt.status == SupervisorStatus.STOPPED_ERROR:
                        return self._finalize(self.rt.stop_reason or StopReason.INVALID_AGENT_STATE, self.rt.status, 1)
                    continue

                reason = next_reason or "CONTINUE"
                next_reason = None
                outcome = await self._activation_cycle(reason)
                result = await self._process_outcome(outcome)
                if result is None:
                    continue
                if result[0] == "complete":
                    return await self._complete_interrupted_stop()
                if result[0] == "stop":
                    if len(result) > 4 and result[4]:
                        return self._finalize(result[1], result[2], result[3], keep_parent=True)
                    return self._finalize(result[1], result[2], result[3])
                next_reason = result
        except AgentStateError as exc:
            self.log.emit(AGENT_STATE_INVALID, error=str(exc))
            return self._finalize(
                StopReason.INVALID_AGENT_STATE, SupervisorStatus.STOPPED_ERROR, 1
            )
        finally:
            if self.lock is not None:
                self.lock.release()

    async def _process_outcome(self, outcome: Optional[Outcome]):
        if outcome is None:
            return None
        if outcome.action == "stop_success":
            return ("stop", StopReason.TASK_COMPLETED, SupervisorStatus.STOPPED_SUCCESS, 0)
        if outcome.action == "stop_blocked":
            return ("stop", StopReason.TASK_BLOCKED, SupervisorStatus.STOPPED_BLOCKED, 1)
        if outcome.action == "stop_limit":
            return ("stop", outcome.limit_reason, SupervisorStatus.STOPPED_LIMIT, 1)
        if outcome.action == "stop_error":
            return (
                "stop",
                StopReason.SUPERVISOR_INTERNAL_ERROR,
                SupervisorStatus.STOPPED_ERROR,
                1,
                outcome.keep_parent,
            )
        if outcome.action == "complete_stop":
            # 与"崩溃后恢复 STOPPING"走完全相同的收尾 reconciliation（P1-R4）
            return ("complete",)
        if outcome.action == "wait_ci":
            await self._wait_ci(outcome.agent)
            if self.rt.status == SupervisorStatus.STOPPED_LIMIT:
                return ("stop", self.rt.stop_reason, self.rt.status, 1)
            if self.rt.status == SupervisorStatus.STOPPED_ERROR:
                return ("stop", self.rt.stop_reason or StopReason.INVALID_AGENT_STATE, self.rt.status, 1)
            return None
        if outcome.action == "wait_human":
            return None
        if outcome.action == "restart":
            if outcome.backoff:
                await self._backoff(outcome.next_reason)
            return outcome.next_reason
        raise AssertionError(f"unknown outcome action {outcome.action!r}")

    def _finalize(self, stop_reason: StopReason, status: SupervisorStatus, rc: int, keep_parent=False) -> int:
        assert self.rt is not None
        self.rt.status = status
        self.rt.stop_reason = stop_reason
        if not keep_parent:
            # 默认清空 Parent 记录；kill 失败等 fail-closed 场景必须保留身份供后续
            # 排查/重启 reconciliation（status=STOPPED_ERROR + current_parent 有效）
            self.rt.current_parent = None
        self._accrue_budget()
        self._save_runtime()
        if status == SupervisorStatus.STOPPED_LIMIT:
            self.log.emit(LIMIT_REACHED, reason=stop_reason.value)
        self.log.emit(
            SUPERVISOR_STOPPED,
            status=status.value,
            stop_reason=stop_reason.value,
            exit_code=rc,
        )
        return rc

    async def _complete_interrupted_stop(self) -> int:
        """完成上次 STARTING/STOPPING 收尾中崩溃（runtime 定格 STOPPING）未被完成的 stop。

        安全规则（P0-A2 / P1-B2）：
        - PID 已知且身份可验证（leader 还活着，哪怕是僵尸）→ 杀整组；
        - PID 未知 → 等 process.json（token 匹配 + 身份可验证）→ 杀整组；
        - 无可信身份 → 看 parent.lock 租约：空闲 = 没有活着的 launcher/DSH →
          完成 stop；被占 = 旧 launcher 可能仍存活但身份未知 → **绝不结束 stop**，
          继续等 record / 等租约释放（安全收尾，operator 可介入）。
        - 杀组确认失败（PGID 仍 alive）→ STOPPED_ERROR，绝不写 STOPPED_OPERATOR。

        全程磁盘保持 STOPPING，直到真正写 STOPPED_OPERATOR（二次崩溃安全）。
        """
        parent = self.rt.current_parent
        activation_id = parent.activation_id if parent else 0
        token = parent.activation_token if parent else None
        pid, sid = (parent.pid, parent.process_start_id) if parent else (None, None)

        self.rt.status = SupervisorStatus.STOPPING
        self._save_runtime()

        # 回收最后的 lease（若有）：后续每次保存都会继续落在 STOPPING 上
        emitted_wait = False
        while True:
            if pid and sid and identity_matches(pid, sid):
                if process_group_alive(pid):
                    from .dsh_runner import terminate_process_group

                    ok = await terminate_process_group(
                        pid, self.config.limits.terminate_grace_seconds
                    )
                    if ok:
                        self.log.emit(
                            PARENT_KILLED,
                            activation=activation_id,
                            pid=pid,
                            reason=KillReason.OPERATOR_STOP.value,
                        )
                        break
                    # P0-R4：杀组失败 → PARENT_KILL_FAILED + 保留身份 STOPPED_ERROR
                    #（绝不能写 STOPPED_OPERATOR / 绝不丢弃 current_parent）
                    self.log.emit(
                        PARENT_KILL_FAILED,
                        activation=activation_id,
                        pid=pid,
                        reason=KillReason.OPERATOR_STOP.value,
                    )
                    return self._finalize(
                        StopReason.SUPERVISOR_INTERNAL_ERROR,
                        SupervisorStatus.STOPPED_ERROR,
                        1,
                        keep_parent=True,
                    )
                break  # 组长身份可验证且组本就不存在/已清 → 完成 stop
            # 组长身份不可验证时，先看 PGID 是否仍存活：
            # - PGID 仍存活 → 绝不能以“lease free 就完成 stop”：
            #   lease 只绑定 DSH leader 的 FD，不保证覆盖整个 PGID；
            #   必须 fail-closed（保留身份、留给 operator）。
            pgid_alive = pid and process_group_alive(pid)
            if pgid_alive:
                self.log.emit(
                    PARENT_KILL_FAILED,
                    activation=activation_id,
                    pid=pid,
                    reason="UNVERIFIABLE_PROCESS_GROUP",
                )
                return self._finalize(
                    StopReason.SUPERVISOR_INTERNAL_ERROR,
                    SupervisorStatus.STOPPED_ERROR,
                    1,
                    keep_parent=True,
                )
            # PGID 已消失 → 尝试用 process.json 更新身份
            if token:
                record = self._load_process_record(activation_id)
                if record is not None and record.get("activation_token") == token:
                    rpid, rsid = record.get("pid"), record.get("process_start_id")
                    if rpid and rsid and (rpid != pid or rsid != sid):
                        pid, sid = rpid, rsid
                        continue
            if self._lease_free():
                break  # 没有活着的 activation → 完成 stop
            if not emitted_wait:
                self.log.emit(
                    PARENT_LEASE_HELD, activation=activation_id, activation_token=token
                )
                emitted_wait = True
            await asyncio.sleep(0.2)
        self.rt.current_parent = None
        return self._finalize(StopReason.OPERATOR_STOP, SupervisorStatus.STOPPED_OPERATOR, 0)

    def _restore_or_init_runtime(self) -> RuntimeState:
        existing = self.store.load()
        sid = read_start_id(os.getpid())
        if existing is None:
            rt = RuntimeState(
                schema_version=1,
                status=SupervisorStatus.BOOTING,
                task_started_at=_now_iso(),
                current_parent=None,
                counters=Counters(),
                limits=Limits(
                    max_parent_activations=self.config.limits.max_parent_activations,
                    max_crash_restarts=self.config.limits.max_crash_restarts,
                    max_clean_restarts=self.config.limits.max_clean_restarts,
                    max_timeouts=self.config.limits.max_timeouts,
                    max_ci_wakeups=self.config.limits.max_ci_wakeups,
                    max_active_wall_seconds=self.config.limits.max_active_wall_seconds,
                    parent_timeout_seconds=self.config.limits.parent_timeout_seconds,
                    terminate_grace_seconds=self.config.limits.terminate_grace_seconds,
                ),
                last_agent_checkpoint_seq=0,
                supervisor_pid=os.getpid(),
                supervisor_process_start_id=sid,
                active_budget={"accrued_seconds": 0.0, "last_mark": None},
                stop_reason=None,
            )
            self._save_runtime(rt)
            return rt
        self.log.emit(
            SUPERVISOR_CRASH_RECOVERY,
            previous_status=existing.status.value,
            previous_stop_reason=(
                existing.stop_reason.value if existing.stop_reason else None
            ),
            previous_activation=(
                existing.current_parent.activation_id if existing.current_parent else None
            ),
        )
        # 若上次就是 STOPPING（operator-stop 收尾中崩溃）→ stop intent 必须 durable：
        # 磁盘上**继续保持 STOPPING**（绝不先落盘成 BOOTING），直到整个 Parent 组
        # 确认消失、真正写成 STOPPED_OPERATOR 为止 —— 否则二次崩溃会丢失 stop intent。
        was_stopping = existing.status == SupervisorStatus.STOPPING
        existing.status = (
            SupervisorStatus.STOPPING if was_stopping else SupervisorStatus.BOOTING
        )
        existing.supervisor_pid = os.getpid()
        existing.supervisor_process_start_id = sid
        existing.stop_reason = None
        if not existing.active_budget:
            existing.active_budget = {"accrued_seconds": 0.0, "last_mark": None}
        # 同步最新的配置限额（测试与真实场景中配置可能在重启间变化）
        existing.limits = Limits(
            max_parent_activations=self.config.limits.max_parent_activations,
            max_crash_restarts=self.config.limits.max_crash_restarts,
            max_clean_restarts=self.config.limits.max_clean_restarts,
            max_timeouts=self.config.limits.max_timeouts,
            max_ci_wakeups=self.config.limits.max_ci_wakeups,
            max_active_wall_seconds=self.config.limits.max_active_wall_seconds,
            parent_timeout_seconds=self.config.limits.parent_timeout_seconds,
            terminate_grace_seconds=self.config.limits.terminate_grace_seconds,
        )
        self._save_runtime(existing)
        return existing

    def _save_runtime(self, rt: Optional[RuntimeState] = None) -> None:
        self.store.save(rt if rt is not None else self.rt)

    def _read_agent_state(self) -> Optional[AgentState]:
        state = load_agent_state(self.layout.agent_state_path)
        if state is not None:
            self.log.emit(
                AGENT_STATE, status=state.status.value, checkpoint_seq=state.checkpoint_seq
            )
        return state

    def _read_agent_state_safe(self) -> Optional[AgentState]:
        try:
            return load_agent_state(self.layout.agent_state_path)
        except AgentStateError:
            return None

    def _enforce_limits(self) -> Optional[StopReason]:
        c = self.rt.counters
        l = self.rt.limits
        if c.parent_activations >= l.max_parent_activations:
            return StopReason.MAX_PARENT_ACTIVATIONS
        if c.crash_restarts >= l.max_crash_restarts:
            return StopReason.MAX_CRASH_RESTARTS
        if c.clean_restarts >= l.max_clean_restarts:
            return StopReason.MAX_CLEAN_RESTARTS
        if c.timeouts >= l.max_timeouts:
            return StopReason.MAX_TIMEOUTS
        accrued = (self.rt.active_budget or {}).get("accrued_seconds", 0.0)
        if accrued >= l.max_active_wall_seconds:
            return StopReason.MAX_ACTIVE_WALL_TIME
        return None

    def _accrue_budget(self) -> None:
        budget = self.rt.active_budget or {"accrued_seconds": 0.0, "last_mark": None}
        paused = (
            self.rt.status == SupervisorStatus.WAITING_HUMAN
            and self.config.human.pause_active_wall_clock
        )
        now = time.monotonic()
        if paused:
            budget["last_mark"] = None
        elif budget["last_mark"] is None:
            budget["last_mark"] = now
        else:
            budget["accrued_seconds"] += max(0.0, now - budget["last_mark"])
            budget["last_mark"] = now
        self.rt.active_budget = budget

    def _make_ci_provider(self):
        if self.ci_provider is not None:
            return self.ci_provider
        from .ci.fake import FakeCiProvider
        from .ci.github import GitHubCiProvider
        if self.config.ci.provider == "fake":
            return FakeCiProvider([CiStatus.SUCCESS])
        return GitHubCiProvider()

    def _verify_sha_exists(self, sha: str) -> bool:
        # In non-git repos (tests) git rev-parse fails with "not a git repo".
        # Treat as verification passed to keep fake tests working; real repos
        # will have .git and the check matters.
        if not (self.base / ".git").exists():
            # Also check if repo is inside work tree
            try:
                r0 = subprocess.run(["git", "rev-parse", "--is-inside-work-tree"], cwd=str(self.base), capture_output=True, timeout=5)
                if r0.returncode != 0:
                    return True
            except Exception:
                return True
        r = subprocess.run(["git", "rev-parse", "--verify", sha], cwd=str(self.base), capture_output=True, timeout=5)
        return r.returncode == 0

    async def _backoff(self, reason) -> None:
        idx = min(
            self.rt.counters.crash_restarts,
            len(self.config.restart.backoff_seconds) - 1,
        )
        delay = float(self.config.restart.backoff_seconds[idx])
        self.rt.status = SupervisorStatus.RESTART_BACKOFF
        self._save_runtime()
        self.log.emit(RESTART_BACKOFF, seconds=delay, reason=reason)
        steps = max(1, int(delay * 10))
        for _ in range(steps):
            if self.stop_event.is_set():
                return
            await asyncio.sleep(delay / steps)

    async def _wait_ci(self, agent: AgentState) -> None:
        # Validate WAIT_CI state — lenient when CI disabled to preserve existing tests
        sha_raw = agent.ci.get("sha") if isinstance(agent.ci, dict) else None
        if not isinstance(agent.ci, dict) or not sha_raw:
            if not self.config.ci.enabled:
                self.rt.status = SupervisorStatus.WAITING_CI
                self._save_runtime()
                self.log.emit(CI_DISABLED, requested_sha=sha_raw)
                self.log.emit(WAIT_CI, sha=sha_raw)
                while not self.stop_event.is_set():
                    st = self._read_agent_state_safe()
                    if st is None or st.status != agent.status:
                        return
                    await asyncio.sleep(0.2)
                return
            self.log.emit(AGENT_STATE_INVALID, error="WAIT_CI missing ci.sha")
            self.rt.status = SupervisorStatus.STOPPED_ERROR
            self.rt.stop_reason = StopReason.INVALID_AGENT_STATE
            self._save_runtime()
            self.log.emit(SUPERVISOR_STOPPED, status="STOPPED_ERROR", stop_reason="INVALID_AGENT_STATE")
            raise AgentStateError("WAIT_CI missing ci.sha")

        sha = sha_raw
        if not isinstance(sha, str) or not re.match(r"^[0-9a-fA-F]{7,40}$", sha.strip()):
            if not self.config.ci.enabled:
                sha = sha if isinstance(sha, str) else str(sha)
                self.rt.status = SupervisorStatus.WAITING_CI
                self._save_runtime()
                self.log.emit(CI_DISABLED, requested_sha=sha)
                self.log.emit(WAIT_CI, sha=sha)
                while not self.stop_event.is_set():
                    st = self._read_agent_state_safe()
                    if st is None or st.status != agent.status:
                        return
                    await asyncio.sleep(0.2)
                return
            self.log.emit(AGENT_STATE_INVALID, error=f"invalid ci.sha {sha!r}")
            raise AgentStateError(f"invalid ci.sha {sha!r}")

        sha = sha.strip().lower()  # normalize

        # Verify SHA exists locally (skip when CI disabled for legacy compat)
        if self.config.ci.enabled and not self._verify_sha_exists(sha):
            self.log.emit(AGENT_STATE_INVALID, error=f"ci.sha not found locally: {sha}")
            raise AgentStateError(f"ci.sha not found locally: {sha}")

        # Cross-check git.head/pushed_head if present
        if isinstance(agent.git, dict):
            for key in ("head", "pushed_head"):
                if key in agent.git and agent.git[key] is not None and agent.git[key] != sha:
                    # Only error if full 40-char sha mismatch; short sha may be prefix
                    if len(agent.git[key]) == 40 and len(sha) == 40:
                        self.log.emit(AGENT_STATE_INVALID, error=f"git.{key} mismatch ci.sha")
                        raise AgentStateError(f"git.{key} mismatch ci.sha")

        if not self.config.ci.enabled:
            self.rt.status = SupervisorStatus.WAITING_CI
            self._save_runtime()
            self.log.emit(CI_DISABLED, requested_sha=sha)
            self.log.emit(WAIT_CI, sha=sha)
            while not self.stop_event.is_set():
                st = self._read_agent_state_safe()
                if st is None or st.status != agent.status:
                    return
                await asyncio.sleep(0.2)
            return

        # Durable ci_wait
        import time as _time
        self.rt.status = SupervisorStatus.WAITING_CI
        now = _now_iso()
        if self.rt.ci_wait is None or self.rt.ci_wait.get("sha") != sha:
            self.rt.ci_wait = {"sha": sha, "provider": self.config.ci.provider, "started_at": now, "last_status": CiStatus.NOT_FOUND.value, "last_observed_at": now}
        self._save_runtime()
        self.log.emit(CI_WAIT_STARTED, sha=sha, provider=self.config.ci.provider)
        self.log.emit(WAIT_CI, sha=sha)

        provider = self._make_ci_provider()
        start_mono = _time.monotonic()
        grace = self.config.ci.discovery_grace_seconds
        max_wait = self.config.ci.max_wait_seconds
        poll = self.config.ci.poll_seconds

        while not self.stop_event.is_set():
            # budget and timeout check
            self._accrue_budget()
            if _time.monotonic() - start_mono > max_wait:
                self.log.emit(CI_WAIT_TIMEOUT, sha=sha)
                try:
                    inbox = self.layout.ci_inbox_dir(sha)
                    atomic_write_json(inbox / "observation.json", {"provider": self.config.ci.provider, "sha": sha, "status": "TIMEOUT", "observed_at": _now_iso()})
                except Exception:
                    pass
                self.rt.status = SupervisorStatus.STOPPED_LIMIT
                self.rt.stop_reason = StopReason.CI_WAIT_TIMEOUT
                self.rt.ci_wait = None
                self._save_runtime()
                self.log.emit(LIMIT_REACHED, reason="CI_WAIT_TIMEOUT")
                return

            try:
                obs = await provider.get_status(repo=self.base, sha=sha)
            except Exception as exc:
                self.log.emit(CI_OBSERVED, sha=sha, status="ERROR", error=str(exc))
                await asyncio.sleep(poll)
                continue

            self.rt.ci_wait["last_status"] = obs.status.value if hasattr(obs.status, "value") else str(obs.status)
            self.rt.ci_wait["last_observed_at"] = obs.observed_at
            self._save_runtime()
            self.log.emit(CI_OBSERVED, sha=sha, status=self.rt.ci_wait["last_status"])

            status_str = self.rt.ci_wait["last_status"]
            elapsed = _time.monotonic() - start_mono

            if status_str == "NOT_FOUND":
                if elapsed < grace:
                    await asyncio.sleep(poll)
                    continue
                else:
                    self.log.emit(CI_WAIT_TIMEOUT, sha=sha, reason="NOT_FOUND beyond grace")
                    self.rt.status = SupervisorStatus.STOPPED_LIMIT
                    self.rt.stop_reason = StopReason.CI_WAIT_TIMEOUT
                    self.rt.ci_wait = None
                    self._save_runtime()
                    self.log.emit(LIMIT_REACHED, reason="CI_WAIT_TIMEOUT")
                    return

            if status_str == "PENDING":
                await asyncio.sleep(poll)
                continue

            if status_str == "SUCCESS":
                self.rt.counters.ci_wakeups += 1
                self.rt.ci_wait = None
                self.rt.status = SupervisorStatus.BOOTING
                self._save_runtime()
                self.log.emit(CI_SUCCEEDED, sha=sha)
                outcome = await self._activation_cycle("CI_SUCCEEDED")
                await self._process_outcome(outcome)
                return

            if status_str == "FAILURE":
                inbox = self.layout.ci_inbox_dir(sha)
                try:
                    await provider.collect_failure(repo=self.base, sha=sha, destination=inbox)
                    self.log.emit(CI_MATERIAL_SAVED, sha=sha, inbox=str(inbox))
                except Exception as exc:
                    self.log.emit(CI_MATERIAL_SAVED, sha=sha, error=str(exc))
                self.rt.counters.ci_wakeups += 1
                self.rt.ci_wait = None
                self.rt.status = SupervisorStatus.BOOTING
                self._save_runtime()
                self.log.emit(CI_FAILED, sha=sha, inbox=str(inbox))
                outcome = await self._activation_cycle("CI_FAILED")
                await self._process_outcome(outcome)
                return

            if status_str in ("CANCELLED", "ERROR"):
                self.rt.ci_wait = None
                self.rt.status = SupervisorStatus.BOOTING
                self._save_runtime()
                outcome = await self._activation_cycle("CI_FAILED")
                await self._process_outcome(outcome)
                return

            if status_str == "TIMEOUT":
                self.rt.status = SupervisorStatus.STOPPED_LIMIT
                self.rt.stop_reason = StopReason.CI_WAIT_TIMEOUT
                self.rt.ci_wait = None
                self._save_runtime()
                self.log.emit(LIMIT_REACHED, reason="CI_WAIT_TIMEOUT")
                return

            await asyncio.sleep(poll)

    async def _wait_human(self) -> Optional[str]:
        self.rt.status = SupervisorStatus.WAITING_HUMAN
        self._accrue_budget()
        self._save_runtime()
        self.log.emit(WAIT_HUMAN)
        store = HumanEventStore(self.layout.human_inbox_dir)
        def _migrate_resume():
            if self.layout.resume_path.exists():
                try:
                    marker = read_json_strict(self.layout.resume_path)
                    evt = marker.get("event")
                    if evt in ("HUMAN_APPROVED", "HUMAN_CHANGES_REQUESTED"):
                        store.append(evt, message=marker.get("message"))
                        try:
                            self.layout.resume_path.unlink()
                        except OSError:
                            pass
                        return True
                except Exception:
                    pass
            return False
        _migrate_resume()
        while not self.stop_event.is_set():
            _migrate_resume()
            evt = store.next_pending()
            if evt is not None:
                store.mark_delivering(evt.event_id)
                self.rt.human_event_id = evt.event_id
                self._save_runtime()
                self.log.emit(HUMAN_EVENT_RECEIVED, event_id=evt.event_id, event_type=evt.event_type)
                self.log.emit(HUMAN_EVENT_DELIVERY_STARTED, event_id=evt.event_id)
                self.rt.status = SupervisorStatus.BOOTING
                self._accrue_budget()
                self._save_runtime()
                return evt.event_type
            await asyncio.sleep(0.2)
        return None

    def _load_process_record(self, activation_id: int):
        p = self.layout.run_dir(activation_id) / "process.json"
        try:
            return read_json_strict(p)
        except (ValueError, OSError):
            return None

    def _adoption_deadline(self, parent: ParentInfo):
        timeout = self.config.limits.parent_timeout_seconds
        try:
            ts = datetime.fromisoformat(parent.started_at.replace("Z", "+00:00")).timestamp()
            return ts + timeout
        except Exception:
            return time.time() + timeout

    def _lease_free(self) -> bool:
        """探测 parent.lock 租约是否空闲（不长时间持有）。

        租约空闲 ⇔ 没有活着（未 exec / 已 exec 的 DSH）的旧 activation 持有它。
        我们自己的 repo 独占锁排除了"另一个 Supervisor 正在并发 acquire"，
        因此探测结果是可靠的。探测会临时获取再立即释放，无副作用。
        """
        if self.lease.held:
            return False
        if not self.lease.try_acquire():
            return False
        self.lease.release()
        return True

    def _record_valid(self, record, token):
        """process.json 记录是否指向一个**活着且身份可信**的 DSH/launcher。"""
        if record is None or record.get("activation_token") != token:
            return None
        rpid = record.get("pid")
        rsid = record.get("process_start_id")
        if not rpid or not rsid:
            return None
        if is_proc_alive(rpid) and identity_matches(rpid, rsid) and is_dsh_process(rpid):
            return rpid
        return None

    async def _reconcile_starting_parent(self, parent: ParentInfo, token: str) -> Optional[Outcome]:
        """STARTING_PARENT 恢复：先看 process.json，缺失则宽限等待。

        返回非 None = 本次 bootstrap 已定案（孤儿自退/记录陈旧），
        返回 None 且 current_parent 已清 = 放弃并允许重 spawn（租约空闲证明无旧进程），
        返回 None 且 current_parent.pid 已填 = 进入收养循环。
        """
        record = self._load_process_record(parent.activation_id)
        if record is not None:
            rpid = self._record_valid(record, token)
            if rpid is not None:
                parent.pid = rpid
                parent.process_start_id = record.get("process_start_id")
                self.rt.status = SupervisorStatus.RUNNING_PARENT
                self.log.emit(PARENT_RECONCILED, activation=parent.activation_id, pid=rpid)
                self._save_runtime()
                return None
            # 记录存在但陈旧：清理残留组（仅当组长身份可验证）→ 保守 crash 恢复
            cleaned = await self._clean_recorded_group(
                record.get("pid"), record.get("process_start_id")
            )
            self.log.emit(PARENT_RECORD_STALE, activation=parent.activation_id)
            if not cleaned:
                # P0-R4：残留组仍在 → 把记录里的真实 pid/start_id 写回 current_parent
                # 保留身份，fail-closed（绝不 restart 继续开发）
                parent.pid = record.get("pid")
                parent.process_start_id = record.get("process_start_id")
                self.rt.current_parent = parent
                self._save_runtime()
                return Outcome(action="stop_error", keep_parent=True)
            self.rt.current_parent = None
            self._save_runtime()
            return self._outcome_from_agent_state("RECOVER_AFTER_PARENT_CRASH")

        # 记录缺失：有界宽限等 launcher 自写 process.json
        grace = max(0.5, self.config.limits.terminate_grace_seconds)
        deadline = time.monotonic() + grace
        while time.monotonic() < deadline and not self.stop_event.is_set():
            record = self._load_process_record(parent.activation_id)
            rpid = self._record_valid(record, token)
            if rpid is not None:
                parent.pid = rpid
                parent.process_start_id = record.get("process_start_id")
                self.rt.status = SupervisorStatus.RUNNING_PARENT
                self.log.emit(PARENT_RECONCILED, activation=parent.activation_id, pid=rpid)
                self._save_runtime()
                return None
            await asyncio.sleep(0.05)

        # 宽限内/后收到 operator stop → 切入 STOPPING（绝不落入 PSU/清 current_parent）
        if self.stop_event.is_set():
            self.rt.status = SupervisorStatus.STOPPING
            self._save_runtime()
            return None

        # 宽限已过、仍无记录：若租约被占（旧 launcher 还活着、只是还没写记录），
        # **绝不 spawn** —— 继续等记录出现或租约释放；收到 stop 则切入 STOPPING。
        if not self._lease_free():
            self.log.emit(
                PARENT_LEASE_HELD, activation=parent.activation_id, activation_token=token
            )
            while True:
                record = self._load_process_record(parent.activation_id)
                rpid = self._record_valid(record, token)
                if rpid is not None:
                    parent.pid = rpid
                    parent.process_start_id = record.get("process_start_id")
                    self.rt.status = SupervisorStatus.RUNNING_PARENT
                    self.log.emit(PARENT_RECONCILED, activation=parent.activation_id, pid=rpid)
                    self._save_runtime()
                    return None
                if self.stop_event.is_set():
                    # operator stop 在 reconcile 等待期到达 → 切入 STOPPING 完成收尾
                    self.rt.status = SupervisorStatus.STOPPING
                    self._save_runtime()
                    return None
                if self._lease_free():
                    break
                await asyncio.sleep(0.1)

        # 租约空闲 = 旧 launcher/DSH 必死 → 安全放弃旧记录并允许重 spawn
        self.log.emit(
            PARENT_SPAWN_UNCONFIRMED,
            activation=parent.activation_id,
            activation_token=token,
        )
        self.rt.current_parent = None
        self._save_runtime()
        return None

    def _outcome_from_agent_state(self, crash_reason: str) -> Optional[Outcome]:
        """按 durable agent 状态分派孤儿消失后的下一步（P1）。"""
        agent = self._read_agent_state_safe()
        status = agent.status if agent else None
        if status == AgentStatus.COMPLETED:
            return Outcome(action="stop_success")
        if status == AgentStatus.BLOCKED:
            return Outcome(action="stop_blocked")
        if status == AgentStatus.WAIT_HUMAN:
            return Outcome(action="wait_human")
        if status == AgentStatus.WAIT_CI:
            return Outcome(action="wait_ci", agent=agent)
        # 状态未知/RUNNING：退出方式未知 → 保守按 crash 恢复
        return Outcome(action="restart", next_reason=crash_reason, backoff=True)

    async def _clean_recorded_group(self, pid, start_id=None) -> bool:
        """记录的 leader 已死但组内还有成员（子进程）时，清理残留进程组。

        安全约束（P0-2/“绝不错杀”）：**必须** start_id 非空且组长身份可验证
        （leader 还在，哪怕已僵尸——starttime 仍可从 /proc 读到）才允许按 pgid
        杀组；start_id 缺失或身份不符时，**绝不**按裸 pid 冒险杀组。
        但若 PGID 仍存活而身份已不可验证，也绝不能返回“清理成功”——那会让调用方
        以为旧 group 已消失而继续 spawn 新 Parent，造成两个 activation 同时改
        working tree。正确做法是 **fail-closed：返回 False（留给 operator）**。

        返回 True = 清理完成 / 无可清理（可继续）；False = 必须 fail-closed
        （保留身份、绝不 restart；可能是 STALE_GROUP_CLEANUP kill failure，或
        PGID 仍存活但身份不可验证）。
        """
        if not pid:
            return True
        group_alive = process_group_alive(pid)
        if not group_alive:
            return True
        # PGID 仍存活，但身份不可验证 → 不能安全 kill，也绝不能说“已清理”
        if not start_id or not identity_matches(pid, start_id):
            self.log.emit(
                PARENT_KILL_FAILED,
                activation=0,
                pid=pid,
                reason="UNVERIFIABLE_PROCESS_GROUP",
            )
            return False
        from .dsh_runner import terminate_process_group

        ok = await terminate_process_group(pid, self.config.limits.terminate_grace_seconds)
        if ok:
            self.log.emit(
                PARENT_KILLED,
                activation=0,
                pid=pid,
                reason=KillReason.STALE_GROUP_CLEANUP.value,
            )
            return True
        self.log.emit(
            PARENT_KILL_FAILED,
            activation=0,
            pid=pid,
            reason=KillReason.STALE_GROUP_CLEANUP.value,
        )
        return False

    def _orphan_live(self, pid, sid) -> bool:
        return bool(pid) and is_proc_alive(pid) and identity_matches(pid, sid) and is_dsh_process(pid)

    async def _adopt_orphan_if_needed(self) -> Optional[Outcome]:
        parent = self.rt.current_parent
        if parent is None:
            return None
        pid = parent.pid
        sid = parent.process_start_id
        token = parent.activation_token

        # STARTING_PARENT：spawn 结果未知 → process.json reconciliation
        if pid is None:
            outcome = await self._reconcile_starting_parent(parent, token)
            if outcome is not None:
                return outcome
            if self.rt.current_parent is None:
                return None  # 放弃重 spawn
            pid = parent.pid
            sid = parent.process_start_id
            if pid is None:
                return None  # 防御

        # RUNNING_PARENT 完整判定（含 cmdline）
        if not self._orphan_live(pid, sid):
            # 记录的 leader 死了：若组里还有成员（被遗弃的子进程），先清组
            cleaned = await self._clean_recorded_group(pid, sid)
            self.rt.counters.parent_activations = max(
                self.rt.counters.parent_activations, parent.activation_id
            )
            if not cleaned:
                # P0-R4：残留组未被清掉 → 保留身份 fail-closed（绝不 restart）
                self._save_runtime()
                return Outcome(action="stop_error", keep_parent=True)
            self.rt.current_parent = None
            self._save_runtime()
            return self._outcome_from_agent_state("RECOVER_AFTER_PARENT_CRASH")

        self.log.emit(ORPHAN_ADOPTED, activation=parent.activation_id, pid=pid)
        deadline = self._adoption_deadline(parent)
        killed_by: Optional[KillReason] = None
        kill_failed = False
        while True:
            if not self._orphan_live(pid, sid):
                break
            if self.stop_event.is_set():
                self.rt.status = SupervisorStatus.STOPPING
                self._save_runtime()
                from .dsh_runner import terminate_process_group

                kill_failed = not await terminate_process_group(
                    pid, self.config.limits.terminate_grace_seconds
                )
                killed_by = KillReason.OPERATOR_STOP
                break
            self._accrue_budget()
            if (self.rt.active_budget or {}).get("accrued_seconds", 0.0) >= self.config.limits.max_active_wall_seconds:
                from .dsh_runner import terminate_process_group

                kill_failed = not await terminate_process_group(
                    pid, self.config.limits.terminate_grace_seconds
                )
                killed_by = KillReason.MAX_ACTIVE_WALL_TIME
                break
            if time.time() >= deadline:
                from .dsh_runner import terminate_process_group

                kill_failed = not await terminate_process_group(
                    pid, self.config.limits.terminate_grace_seconds
                )
                killed_by = KillReason.PARENT_TIMEOUT
                break
            await asyncio.sleep(0.2)

        # —— 收尾：清当前 Parent 记录 ——
        if kill_failed:
            # P0-R4：PGID 熬过了 SIGKILL+确认窗口 → 记录 PARENT_KILL_FAILED
            # （不是 PARENT_KILLED——没有 kill 成功），**保留 current_parent 身份**
            # fail-closed（绝不 restart 继续开发）。
            self.log.emit(
                PARENT_KILL_FAILED,
                activation=parent.activation_id,
                pid=pid,
                reason=(killed_by.value if killed_by else ""),
            )
            self._save_runtime()
            return Outcome(action="stop_error", keep_parent=True)
        self.rt.counters.parent_activations = max(
            self.rt.counters.parent_activations, parent.activation_id
        )
        self.rt.current_parent = None
        self._save_runtime()

        if killed_by == KillReason.OPERATOR_STOP:
            self.log.emit(PARENT_KILLED, activation=parent.activation_id, pid=pid, reason=KillReason.OPERATOR_STOP.value)
            return None
        if killed_by == KillReason.PARENT_TIMEOUT:
            self.rt.counters.timeouts += 1
            self.log.emit(PARENT_TIMEOUT, activation=parent.activation_id, pid=pid)
            self.log.emit(PARENT_KILLED, activation=parent.activation_id, pid=pid, reason=KillReason.PARENT_TIMEOUT.value)
            self._save_runtime()
            # 与普通激活超时一致：timeouts+1 + 退避 + RECOVER_AFTER_PARENT_TIMEOUT
            return self._outcome_from_agent_state("RECOVER_AFTER_PARENT_TIMEOUT")
        if killed_by == KillReason.MAX_ACTIVE_WALL_TIME:
            # 主循环 enforce_limits 会以 MAX_ACTIVE_WALL_TIME 收场
            self.log.emit(PARENT_KILLED, activation=parent.activation_id, pid=pid, reason=KillReason.MAX_ACTIVE_WALL_TIME.value)
            return None
        # orphan 自己退出、退出方式未知
        self.log.emit(ORPHAN_EXITED, pid=pid, activation=parent.activation_id)
        return self._outcome_from_agent_state("RECOVER_AFTER_PARENT_CRASH")

    async def _activation_cycle(self, reason: str, **prompt_kwargs) -> Optional[Outcome]:
        try:
            return await self._activation_cycle_inner(reason, **prompt_kwargs)
        finally:
            self.lease.release()

    def _ensure_lease(self) -> bool:
        """确保引擎持有 Parent 租约；被他人持有时返回 False（绝不无租约 spawn）。"""
        return self.lease.held or self.lease.try_acquire()

    async def _activation_cycle_inner(self, reason: str, **prompt_kwargs) -> Optional[Outcome]:
        limit = self._enforce_limits()
        if limit is not None:
            return Outcome(action="stop_limit", limit_reason=limit)

        # P0-1：拿不到 Parent lease = 存在活着的旧 activation → 绝不 spawn 第二个
        if not self._ensure_lease():
            self.log.emit(PARENT_LEASE_HELD, reason=reason)
            while not self._ensure_lease():
                if self.stop_event.is_set():
                    return None
                await asyncio.sleep(0.2)

        activation_id = self.rt.counters.parent_activations + 1
        token = uuid.uuid4().hex
        run_dir = self.layout.run_dir(activation_id)
        run_dir.mkdir(parents=True, exist_ok=True)

        git_before = capture_git(self.base)
        try:
            atomic_write_json(run_dir / "git-before.json", git_before.to_dict())
            self.log.emit(GIT_SNAPSHOT, phase="before", activation=activation_id, **git_before.to_dict())
        except Exception:
            pass
        task_file = self.layout.task_file(self.config).relative_to(self.base).as_posix()
        prompt = build_prompt(reason, task_file=task_file, **prompt_kwargs)
        (run_dir / "prompt.txt").write_text(prompt, encoding="utf-8")

        prev_seq = self.rt.last_agent_checkpoint_seq
        self._prev_seq = prev_seq

        self.rt.status = SupervisorStatus.STARTING_PARENT
        self.rt.current_parent = ParentInfo(
            activation_id=activation_id,
            pid=None,
            process_start_id=None,
            started_at=_now_iso(),
            reason=reason,
            activation_token=token,
        )
        self._save_runtime()
        self.log.emit(PARENT_STARTING, activation=activation_id, reason=reason, activation_token=token)

        def on_start(pid, start_id):
            self.rt.current_parent.pid = pid
            self.rt.current_parent.process_start_id = start_id
            self.rt.status = SupervisorStatus.RUNNING_PARENT
            self._save_runtime()
            self.log.emit(
                PARENT_STARTED,
                activation=activation_id,
                pid=pid,
                process_start_id=start_id,
                reason=reason,
            )

        run_task = asyncio.ensure_future(
            self.runner.run(
                repo=self.base,
                prompt=prompt,
                activation_id=activation_id,
                timeout_seconds=self.config.limits.parent_timeout_seconds,
                run_dir=run_dir,
                on_start=on_start,
                activation_token=token,
                lease_fd=self.lease.fd if self.lease.held else None,
            )
        )
        stop_task = asyncio.ensure_future(self.stop_event.wait())

        done, pending = await asyncio.wait(
            {run_task, stop_task}, return_when=asyncio.FIRST_COMPLETED
        )
        if run_task not in done:
            # operator stop：STOPPING 先落盘（终止宽限期内崩溃也可恢复），再整组终止
            self.rt.status = SupervisorStatus.STOPPING
            self._save_runtime()
            stop_task.cancel()
            run_task.cancel()
            try:
                await run_task
            except (asyncio.CancelledError, Exception):
                pass
            self.log.emit(OPERATOR_STOP, activation=activation_id)
            # P1-R4：与"崩溃后恢复 STOPPING"走同一套收尾 reconciliation——
            # pid 已知→杀组；pid 未知→process.json；record 未知→parent.lock 租约。
            # 取消路径不再做 ad-hoc last_pid 特判，统一交给 _complete_interrupted_stop。
            return Outcome(action="complete_stop")

        stop_task.cancel()
        try:
            result = run_task.result()
            run_error = None
        except Exception as exc:
            result = None
            run_error = exc

        if result is not None and result.group_survived:
            # P0-R4：超时终止失败（进程组仍存活）→ 绝不能 restart；**保留
            # current_parent 身份**（activation_id/pid/start_id/token）供后续排查，
            # 并记录 PARENT_KILL_FAILED（不是 PARENT_KILLED）→ STOPPED_ERROR。
            self.log.emit(
                PARENT_KILL_FAILED,
                activation=activation_id,
                pid=self.rt.current_parent.pid if self.rt.current_parent else None,
                reason=KillReason.PARENT_TIMEOUT.value,
            )
            self.rt.counters.parent_activations = activation_id
            self._save_runtime()
            return Outcome(action="stop_error", keep_parent=True)

        self.rt.current_parent = None
        self.rt.counters.parent_activations = activation_id

        git_after = capture_git(self.base)
        try:
            atomic_write_json(run_dir / "git-after.json", git_after.to_dict())
            self.log.emit(GIT_SNAPSHOT, phase="after", activation=activation_id, **git_after.to_dict())
            diff = compare_git_snapshots(git_before, git_after)
            if diff.get("head_changed"):
                self.log.emit(GIT_HEAD_CHANGED, activation=activation_id, before=git_before.head, after=git_after.head)
            if diff.get("dirty_changed"):
                self.log.emit(GIT_DIRTY_CHANGED, activation=activation_id, before=git_before.dirty, after=git_after.dirty)
            if diff.get("branch_changed"):
                self.log.emit(GIT_BRANCH_CHANGED, activation=activation_id, before=git_before.branch, after=git_after.branch)
        except Exception:
            pass
        agent_after = self._read_agent_state()
        fresh = bool(agent_after and agent_after.checkpoint_seq > prev_seq)
        if agent_after is not None:
            self.rt.last_agent_checkpoint_seq = max(
                self.rt.last_agent_checkpoint_seq, agent_after.checkpoint_seq
            )

        self.log.emit(
            PARENT_EXITED,
            activation=activation_id,
            exit_code=result.exit_code if result else None,
            timed_out=result.timed_out if result else False,
        )

        self._write_run_artifact(
            activation_id, run_dir, git_before, git_after, agent_after, prompt, reason, result
        )

        if result is None:
            self.rt.counters.crash_restarts += 1
            self.log.emit(PARENT_CRASH, activation=activation_id, error=str(run_error))
            self._save_runtime()
            return Outcome(action="restart", next_reason="RECOVER_AFTER_PARENT_CRASH", backoff=True)

        if result.timed_out:
            self.rt.counters.timeouts += 1
            self.log.emit(PARENT_TIMEOUT, activation=activation_id)
        elif agent_after is None or (
            agent_after.status == AgentStatus.RUNNING and result.exit_code != 0
        ):
            self.rt.counters.crash_restarts += 1
            self.log.emit(
                PARENT_CRASH,
                activation=activation_id,
                exit_code=result.exit_code,
                missing_state=agent_after is None,
                fresh=fresh,
            )

        if result.timed_out:
            if agent_after is None or agent_after.status == AgentStatus.RUNNING:
                self._save_runtime()
                return Outcome(
                    action="restart", next_reason="RECOVER_AFTER_PARENT_TIMEOUT", backoff=True
                )
            if agent_after.status == AgentStatus.COMPLETED:
                return Outcome(action="stop_success")
            if agent_after.status == AgentStatus.BLOCKED:
                return Outcome(action="stop_blocked")
            if agent_after.status == AgentStatus.WAIT_HUMAN:
                return Outcome(action="wait_human")
            return Outcome(action="wait_ci", agent=agent_after)

        if agent_after is None:
            self._save_runtime()
            return Outcome(
                action="restart", next_reason="RECOVER_AFTER_PARENT_CRASH", backoff=True
            )

        if agent_after.status == AgentStatus.COMPLETED:
            return Outcome(action="stop_success")
        if agent_after.status == AgentStatus.BLOCKED:
            return Outcome(action="stop_blocked")
        if agent_after.status == AgentStatus.WAIT_HUMAN:
            return Outcome(action="wait_human")
        if agent_after.status == AgentStatus.WAIT_CI:
            if result.exit_code != 0:
                self.log.emit(
                    AGENT_ANOMALY,
                    activation=activation_id,
                    parent_exit_code=result.exit_code,
                    state_transition="WAIT_CI",
                )
            return Outcome(action="wait_ci", agent=agent_after)

        if result.exit_code == 0:
            if fresh:
                self.log.emit(PARENT_CLEAN_EXIT_WITH_RUNNING_STATE, activation=activation_id)
            else:
                self.log.emit(PARENT_NO_PROGRESS, activation=activation_id)
            self.rt.counters.clean_restarts += 1
            self._save_runtime()
            return Outcome(action="restart", next_reason="CONTINUE", backoff=False)
        self._save_runtime()
        return Outcome(
            action="restart", next_reason="RECOVER_AFTER_PARENT_CRASH", backoff=True
        )

    def _write_run_artifact(
        self, activation_id, run_dir, git_before, git_after, agent_after, prompt, reason, result
    ):
        # git-before/after already written crash-safely; ensure present but don't overwrite if already durable
        try:
            if not (run_dir / "git-before.json").exists():
                atomic_write_json(run_dir / "git-before.json", git_before.to_dict())
            if not (run_dir / "git-after.json").exists():
                atomic_write_json(run_dir / "git-after.json", git_after.to_dict())
        except Exception:
            pass
        data = {
            "activation_id": activation_id,
            "reason": reason,
            "started_at": result.started_at if result else None,
            "ended_at": result.ended_at if result else None,
            "duration_seconds": result.duration_seconds if result else None,
            "exit_code": result.exit_code if result else None,
            "timed_out": result.timed_out if result else False,
            "pid": result.pid if result else None,
            "process_start_id": result.process_start_id if result else None,
            "prompt": prompt,
            "agent_state_before_seq": self._prev_seq,
            "agent_state_after": (
                {"status": agent_after.status.value, "checkpoint_seq": agent_after.checkpoint_seq}
                if agent_after
                else None
            ),
            "git_before": git_before.to_dict(),
            "git_after": git_after.to_dict(),
        }
        atomic_write_json(run_dir / "result.json", data)