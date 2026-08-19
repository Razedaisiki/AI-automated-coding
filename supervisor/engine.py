"""Supervisor 引擎主循环（M3/M4/M5 hardening）。

- 三态恢复：NO_PARENT / STARTING_PARENT(pid=None + token) / RUNNING_PARENT(pid+start_id)
- STARTING_PARENT 通过 launcher 自写的 process.json 进行 reconciliation（有界宽限）
- PARENT_STARTING → PARENT_STARTED 拆分审计
- 收养期间：stop→杀进程组，parent timeout / wall-time 继续生效，cmdline 校验接入
- supervisor 自身 PID 身份写入 runtime
"""

import asyncio
import os
import signal
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
from .git_probe import capture as capture_git
from .lock import LockHeldError, SupervisorLock
from .models import (
    AgentState,
    AgentStateError,
    AgentStatus,
    Counters,
    Limits,
    ParentInfo,
    RuntimeState,
    StopReason,
    SupervisorStatus,
)
from .process_identity import identity_matches, is_dsh_process, is_proc_alive, read_start_id
from .prompts import build_prompt
from .storage import Layout, RuntimeStore, atomic_write_json, load_agent_state, read_json_strict


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class Outcome:
    action: str  # restart | wait_ci | wait_human | stop_success | stop_blocked | stop_limit
    next_reason: Optional[str] = None
    backoff: bool = False
    agent: Optional[AgentState] = None
    limit_reason: Optional[StopReason] = None


class SupervisorEngine:
    def __init__(self, base_dir, config: Optional[Config] = None, runner=None):
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
        self.stop_event_proxy = None
        self._stop_requested = False
        self.lock = None
        self.rt: Optional[RuntimeState] = None

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
        existing_agent = self._read_agent_state_safe()
        if existing_agent is not None:
            self.rt.last_agent_checkpoint_seq = max(
                self.rt.last_agent_checkpoint_seq, existing_agent.checkpoint_seq
            )
            self._save_runtime()
        self.log.emit(SUPERVISOR_STARTED, supervisor_pid=os.getpid())
        await self._adopt_orphan_if_needed()

        next_reason: Optional[str] = None
        try:
            while True:
                if self.stop_event.is_set():
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
                    if result[0] == "stop":
                        return self._finalize(*result[1:])
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
                        outcome = await self._activation_cycle(resume)
                        result = await self._process_outcome(outcome)
                        if result is None:
                            continue
                        if result[0] == "stop":
                            return self._finalize(*result[1:])
                        next_reason = result
                    continue
                if agent.status == AgentStatus.WAIT_CI:
                    await self._wait_ci(agent)
                    continue

                reason = next_reason or "CONTINUE"
                next_reason = None
                outcome = await self._activation_cycle(reason)
                result = await self._process_outcome(outcome)
                if result is None:
                    continue
                if result[0] == "stop":
                    return self._finalize(*result[1:])
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
        if outcome.action == "wait_ci":
            await self._wait_ci(outcome.agent)
            return None
        if outcome.action == "wait_human":
            return None
        if outcome.action == "restart":
            if outcome.backoff:
                await self._backoff(outcome.next_reason)
            return outcome.next_reason
        raise AssertionError(f"unknown outcome action {outcome.action!r}")

    def _finalize(self, stop_reason: StopReason, status: SupervisorStatus, rc: int) -> int:
        assert self.rt is not None
        self.rt.status = status
        self.rt.stop_reason = stop_reason
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
        existing.status = SupervisorStatus.BOOTING
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
        self.rt.status = SupervisorStatus.WAITING_CI
        self._save_runtime()
        sha = agent.ci.get("sha") if isinstance(agent.ci, dict) else None
        if not self.config.ci.enabled:
            self.log.emit(CI_DISABLED, requested_sha=sha)
            while not self.stop_event.is_set():
                st = self._read_agent_state_safe()
                if st is None or st.status != agent.status:
                    return
                await asyncio.sleep(0.2)
            return
        raise NotImplementedError("CI provider polling (M7) is not implemented yet")

    async def _wait_human(self) -> Optional[str]:
        self.rt.status = SupervisorStatus.WAITING_HUMAN
        self._accrue_budget()
        self._save_runtime()
        self.log.emit(WAIT_HUMAN)
        while not self.stop_event.is_set():
            resume = self.layout.resume_path
            if resume.exists():
                try:
                    marker = read_json_strict(resume)
                except ValueError:
                    marker = None
                if marker is not None:
                    event = marker.get("event")
                    if event in ("HUMAN_APPROVED", "HUMAN_CHANGES_REQUESTED"):
                        resume.unlink()
                        self.rt.status = SupervisorStatus.BOOTING
                        self._accrue_budget()
                        self._save_runtime()
                        self.log.emit(RESUME_RECEIVED, resume_event=event)
                        return event
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

    async def _adopt_orphan_if_needed(self) -> None:
        parent = self.rt.current_parent
        if parent is None:
            return
        pid = parent.pid
        sid = parent.process_start_id
        token = parent.activation_token

        # STARTING_PARENT：spawn 结果未知 → 通过 process.json reconciliation
        if pid is None:
            record = self._load_process_record(parent.activation_id)
            if record is not None and record.get("activation_token") == token:
                rpid = record.get("pid")
                rsid = record.get("process_start_id")
                if (
                    rpid
                    and rsid
                    and is_proc_alive(rpid)
                    and identity_matches(rpid, rsid)
                    and is_dsh_process(rpid)
                ):
                    pid, sid = rpid, rsid
                    parent.pid = pid
                    parent.process_start_id = sid
                    self.rt.status = SupervisorStatus.RUNNING_PARENT
                    self.log.emit(PARENT_RECONCILED, activation=parent.activation_id, pid=pid)
                    self._save_runtime()
                else:
                    self.log.emit(PARENT_RECORD_STALE, activation=parent.activation_id)
                    self.rt.current_parent = None
                    self._save_runtime()
                    return
            else:
                grace = max(0.5, self.config.limits.terminate_grace_seconds)
                deadline = time.monotonic() + grace
                reconciled = False
                while time.monotonic() < deadline and not self.stop_event.is_set():
                    record = self._load_process_record(parent.activation_id)
                    if record is not None and record.get("activation_token") == token:
                        rpid = record.get("pid")
                        rsid = record.get("process_start_id")
                        if (
                            rpid
                            and rsid
                            and is_proc_alive(rpid)
                            and identity_matches(rpid, rsid)
                            and is_dsh_process(rpid)
                        ):
                            pid, sid = rpid, rsid
                            parent.pid = pid
                            parent.process_start_id = sid
                            self.rt.status = SupervisorStatus.RUNNING_PARENT
                            self.log.emit(PARENT_RECONCILED, activation=parent.activation_id, pid=pid)
                            self._save_runtime()
                            reconciled = True
                            break
                    await asyncio.sleep(0.05)
                if not reconciled:
                    self.log.emit(
                        PARENT_SPAWN_UNCONFIRMED,
                        activation=parent.activation_id,
                        activation_token=token,
                    )
                    self.rt.current_parent = None
                    self._save_runtime()
                    return
            # 已确认的孤儿，落入下方收养循环
        # RUNNING_PARENT 完整判定（含 cmdline）
        if not (is_proc_alive(pid) and identity_matches(pid, sid) and is_dsh_process(pid)):
            self.rt.counters.parent_activations = max(
                self.rt.counters.parent_activations, parent.activation_id
            )
            self.rt.current_parent = None
            self._save_runtime()
            return

        self.log.emit(ORPHAN_ADOPTED, activation=parent.activation_id, pid=pid)
        deadline = self._adoption_deadline(parent)
        killed_by = None
        while True:
            alive = is_proc_alive(pid) and identity_matches(pid, sid) and is_dsh_process(pid)
            if not alive:
                break
            if self.stop_event.is_set():
                from .dsh_runner import terminate_process_group

                await terminate_process_group(pid, self.config.limits.terminate_grace_seconds)
                killed_by = StopReason.OPERATOR_STOP
                break
            self._accrue_budget()
            if (self.rt.active_budget or {}).get("accrued_seconds", 0.0) >= self.config.limits.max_active_wall_seconds:
                from .dsh_runner import terminate_process_group

                await terminate_process_group(pid, self.config.limits.terminate_grace_seconds)
                killed_by = StopReason.MAX_ACTIVE_WALL_TIME
                break
            if time.time() >= deadline:
                from .dsh_runner import terminate_process_group

                await terminate_process_group(pid, self.config.limits.terminate_grace_seconds)
                killed_by = StopReason.MAX_TIMEOUTS
                break
            await asyncio.sleep(0.2)

        if killed_by == StopReason.OPERATOR_STOP:
            self.log.emit(PARENT_KILLED, activation=parent.activation_id, pid=pid, reason="OPERATOR_STOP")
        elif killed_by == StopReason.MAX_TIMEOUTS:
            self.rt.counters.timeouts += 1
            self.log.emit(PARENT_TIMEOUT, activation=parent.activation_id, pid=pid)
            self.log.emit(PARENT_KILLED, activation=parent.activation_id, pid=pid, reason="PARENT_TIMEOUT")
        elif killed_by == StopReason.MAX_ACTIVE_WALL_TIME:
            self.log.emit(PARENT_KILLED, activation=parent.activation_id, pid=pid, reason="MAX_ACTIVE_WALL_TIME")
        else:
            self.log.emit(ORPHAN_EXITED, pid=pid, activation=parent.activation_id)

        self.rt.counters.parent_activations = max(
            self.rt.counters.parent_activations, parent.activation_id
        )
        self.rt.current_parent = None
        self._save_runtime()

    async def _activation_cycle(self, reason: str) -> Optional[Outcome]:
        limit = self._enforce_limits()
        if limit is not None:
            return Outcome(action="stop_limit", limit_reason=limit)

        activation_id = self.rt.counters.parent_activations + 1
        token = uuid.uuid4().hex
        run_dir = self.layout.run_dir(activation_id)
        run_dir.mkdir(parents=True, exist_ok=True)

        git_before = capture_git(self.base)
        prompt = build_prompt(reason)
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
            )
        )
        stop_task = asyncio.ensure_future(self.stop_event.wait())

        done, pending = await asyncio.wait(
            {run_task, stop_task}, return_when=asyncio.FIRST_COMPLETED
        )
        if run_task not in done:
            stop_task.cancel()
            run_task.cancel()
            try:
                await run_task
            except (asyncio.CancelledError, Exception):
                pass
            self.log.emit(OPERATOR_STOP, activation=activation_id)
            return None

        stop_task.cancel()
        try:
            result = run_task.result()
            run_error = None
        except Exception as exc:
            result = None
            run_error = exc

        self.rt.current_parent = None
        self.rt.counters.parent_activations = activation_id

        git_after = capture_git(self.base)
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
        atomic_write_json(run_dir / "git-before.json", git_before.to_dict())
        atomic_write_json(run_dir / "git-after.json", git_after.to_dict())
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