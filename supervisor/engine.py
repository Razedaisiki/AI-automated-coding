"""Supervisor 引擎主循环（M3/M4/M5）。

纪律：

- Supervisor 决定 WHEN：启动/监控/终止 Parent、限额、等待、终止条件。
- 状态判定永远以 fresh 的 `.agent/state.json` 为准，绝不解析 stdout。
- 每次循环最先 `enforce_limits()`。
- 所有 runtime 写入原子化；当前 Parent 的 pid+startid 在 spawn 后立即持久化，
  以便 Supervisor 自身崩溃后做 orphan 收养。
"""

import asyncio
import os
import time
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
    PARENT_NO_PROGRESS,
    PARENT_STARTED,
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
from .prompts import build_prompt
from .process_identity import identity_matches, is_proc_alive
from .storage import Layout, RuntimeStore, atomic_write_json, load_agent_state, read_json_strict


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class Outcome:
    """一次 activation 后的分派结果。"""

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

    # ------------------------------------------------------------- public

    @property
    def stop_event(self):
        """惰性创建：避免 Python 3.8 下在事件循环外构造 Event 绑定已关闭的 loop。"""
        if self.stop_event_proxy is None:
            self.stop_event_proxy = asyncio.Event()
            if self._stop_requested:
                self.stop_event_proxy.set()
        return self.stop_event_proxy

    def request_stop(self) -> None:
        """操作员停止（SIGINT/SIGTERM 由 CLI 接到后调用）。"""
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
        # 启动时与现有 agent 状态同步 checkpoint 水位（避免把旧 checkpoint 当本轮新产出）
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

                # RUNNING → 跑一轮
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

    # ------------------------------------------------------------ helpers

    async def _process_outcome(self, outcome: Optional[Outcome]):
        """分派一次 activation 的结果。

        返回 ("stop", reason, status, rc) / next_reason / None（继续循环）。
        """
        if outcome is None:
            return None  # 运行中被 operator stop
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
            return None  # 主循环从 state 重新分派
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

    # -------------------------------------------------------- runtime I/O

    def _restore_or_init_runtime(self) -> RuntimeState:
        existing = self.store.load()
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
        existing.stop_reason = None
        if not existing.active_budget:
            existing.active_budget = {"accrued_seconds": 0.0, "last_mark": None}
        self._save_runtime(existing)
        return existing

    def _save_runtime(self, rt: Optional[RuntimeState] = None) -> None:
        self.store.save(rt if rt is not None else self.rt)

    def _read_agent_state(self) -> Optional[AgentState]:
        """读取+校验 .agent/state.json；非法抛 AgentStateError。"""
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

    # ------------------------------------------------------- limits/backoff

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
        """活跃墙钟结算。WAIT_HUMAN 且 pause_active_wall_clock 时暂停。"""
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
        # 可中断的分片睡眠（operator stop 立即生效）
        steps = max(1, int(delay * 10))
        for _ in range(steps):
            if self.stop_event.is_set():
                return
            await asyncio.sleep(delay / steps)

    # ----------------------------------------------------------- wait gates

    async def _wait_ci(self, agent: AgentState) -> None:
        self.rt.status = SupervisorStatus.WAITING_CI
        self._save_runtime()
        sha = agent.ci.get("sha") if isinstance(agent.ci, dict) else None
        if not self.config.ci.enabled:
            self.log.emit(CI_DISABLED, requested_sha=sha)
            # M7 前的安全占位：轮询状态文件变化 / 操作员停止，绝不启动第二个 Parent
            while not self.stop_event.is_set():
                st = self._read_agent_state_safe()
                if st is None or st.status != agent.status:
                    return
                await asyncio.sleep(0.2)
            return
        raise NotImplementedError("CI provider polling (M7) is not implemented yet")

    async def _wait_human(self) -> Optional[str]:
        """WAITING_HUMAN：等待 `supervisor resume --event ...` 或 operator stop。"""
        self.rt.status = SupervisorStatus.WAITING_HUMAN
        self._accrue_budget()  # 结算后暂停预算（mark=None）
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
                        self._accrue_budget()  # 恢复预算（mark=now）
                        self._save_runtime()
                        self.log.emit(RESUME_RECEIVED, resume_event=event)
                        return event
            await asyncio.sleep(0.2)
        return None

    async def _adopt_orphan_if_needed(self) -> None:
        """Supervisor 重启后：记录的 Parent 还活着（pid+starttime 一致）→ 收养。

        收养期间轮询 /proc 等其退出，绝不启动第二个 Parent。
        """
        parent = self.rt.current_parent
        if parent is None or not parent.pid:
            return
        pid = parent.pid
        sid = parent.process_start_id
        if is_proc_alive(pid) and identity_matches(pid, sid):
            self.log.emit(ORPHAN_ADOPTED, activation=parent.activation_id, pid=pid)
            while not self.stop_event.is_set():
                if not (is_proc_alive(pid) and identity_matches(pid, sid)):
                    break
                await asyncio.sleep(0.2)
            self.log.emit(ORPHAN_EXITED, pid=pid, activation=parent.activation_id)
        # 无论死活，激活号不得回退：下一个 activation 从这两者的大值 +1 起算
        self.rt.counters.parent_activations = max(
            self.rt.counters.parent_activations, parent.activation_id
        )
        self.rt.current_parent = None
        self._save_runtime()

    # ------------------------------------------------------ parent lifecycle

    async def _activation_cycle(self, reason: str) -> Optional[Outcome]:
        """启动一轮 Parent 并分类结果。被 operator stop 时返回 None。"""
        limit = self._enforce_limits()
        if limit is not None:
            return Outcome(action="stop_limit", limit_reason=limit)

        activation_id = self.rt.counters.parent_activations + 1
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
        )
        self._save_runtime()
        self.log.emit(PARENT_STARTED, activation=activation_id, reason=reason)

        def on_start(pid, start_id):
            # spawn 后立即持久化进程身份，供 Supervisor 崩溃恢复
            self.rt.current_parent.pid = pid
            self.rt.current_parent.process_start_id = start_id
            self.rt.status = SupervisorStatus.RUNNING_PARENT
            self._save_runtime()

        run_task = asyncio.ensure_future(
            self.runner.run(
                repo=self.base,
                prompt=prompt,
                activation_id=activation_id,
                timeout_seconds=self.config.limits.parent_timeout_seconds,
                run_dir=run_dir,
                on_start=on_start,
            )
        )
        stop_task = asyncio.ensure_future(self.stop_event.wait())

        done, pending = await asyncio.wait(
            {run_task, stop_task}, return_when=asyncio.FIRST_COMPLETED
        )
        if run_task not in done:
            # operator stop：取消 runner（DshRunner 会清进程组）
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
        except Exception as exc:  # RunnerError 等
            result = None
            run_error = exc

        self.rt.current_parent = None
        self.rt.counters.parent_activations = activation_id

        git_after = capture_git(self.base)
        agent_after = self._read_agent_state()  # 可能抛 AgentStateError
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

        # ---- 计数（每个维度只计一次）----
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

        # ---- 分派（fresh 状态优先于进程结果，见协议 9）----
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
                # 文档九：exit1 + fresh WAIT_CI → 仍进 WAIT_CI，记录 anomaly
                self.log.emit(
                    AGENT_ANOMALY,
                    activation=activation_id,
                    parent_exit_code=result.exit_code,
                    state_transition="WAIT_CI",
                )
            return Outcome(action="wait_ci", agent=agent_after)

        # status == RUNNING
        if result.exit_code == 0:
            if fresh:
                self.log.emit(PARENT_CLEAN_EXIT_WITH_RUNNING_STATE, activation=activation_id)
            else:
                self.log.emit(PARENT_NO_PROGRESS, activation=activation_id)
            self.rt.counters.clean_restarts += 1
            self._save_runtime()
            return Outcome(action="restart", next_reason="CONTINUE", backoff=False)
        # 已在上方计入 crash
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