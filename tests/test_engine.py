"""M3/M4/M5 — Supervisor 引擎 FSM 测试（FakeParentRunner，零 LLM）。

覆盖 AI_automated_coding.md 五十一 的 FSM 场景（M3/M4/M5 范围）。
"""

import asyncio
import json
import os
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from supervisor.config import Config, default_config
from supervisor.dsh_runner import DshRunner
from supervisor.engine import SupervisorEngine
from supervisor.events import EventLog
from supervisor.lock import LockHeldError
from supervisor.models import (
    AgentState,
    Counters,
    Limits,
    ParentInfo,
    RuntimeState,
    StopReason,
    SupervisorStatus,
)
from supervisor.process_identity import read_start_id
from supervisor.storage import Layout, RuntimeStore, atomic_write_json

from conftest import (
    FAKE_DSH,
    FakeParentRunner,
    Step,
    StepScript,
    event_names,
    run_engine,
    wait_until,
)


def make_engine(repo, cfg=None, runner=None, layout=None):
    cfg = cfg if cfg is not None else default_config()
    return SupervisorEngine(base_dir=repo, config=cfg, runner=runner)


def fast_cfg(**limit_overrides):
    cfg = default_config()
    cfg.restart.backoff_seconds = [0.01, 0.02, 0.05]
    for k, v in limit_overrides.items():
        setattr(cfg.limits, k, v)
    return cfg


def state_file(repo):
    return repo / ".agent" / "state.json"


def write_agent_state(repo, status="RUNNING", seq=1):
    raw = {
        "schema_version": 1,
        "status": status,
        "checkpoint_seq": seq,
        "updated_at": "2026-08-18T06:00:00Z",
    }
    state_file(repo).parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(state_file(repo), raw)


def write_runtime(repo, *, activation=5, pid=None, start_id=None, counters=None):
    """预写 runtime.json，模拟上次 Supervisor 留下的状态。"""
    layout = Layout(repo)
    c = fast_cfg().limits
    limits = Limits(
        max_parent_activations=c.max_parent_activations,
        max_crash_restarts=c.max_crash_restarts,
        max_clean_restarts=c.max_clean_restarts,
        max_timeouts=c.max_timeouts,
        max_ci_wakeups=c.max_ci_wakeups,
        max_active_wall_seconds=c.max_active_wall_seconds,
        parent_timeout_seconds=c.parent_timeout_seconds,
        terminate_grace_seconds=c.terminate_grace_seconds,
    )
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    rt = RuntimeState(
        schema_version=1,
        status=SupervisorStatus.RUNNING_PARENT,
        task_started_at=now,
        current_parent=(
            ParentInfo(
                activation_id=activation,
                pid=pid,
                process_start_id=start_id,
                started_at=now,
                reason="CONTINUE",
            )
            if pid is not None
            else None
        ),
        counters=counters or Counters(parent_activations=activation - 1, crash_restarts=0),
        limits=limits,
        last_agent_checkpoint_seq=0,
        supervisor_pid=None,
        active_budget={"accrued_seconds": 0.0, "last_mark": None},
        stop_reason=None,
    )
    RuntimeStore(layout).save(rt)


# ------------------------------------------------------------- fresh task


class TestFreshTask:
    def test_01_fresh_task_to_completed(self, tmp_repo):
        cfg = fast_cfg()
        runner = FakeParentRunner([StepScript.completed()], Layout(tmp_repo))
        engine = make_engine(tmp_repo, cfg, runner)
        rc = run_engine(engine)
        assert rc == 0
        names = event_names(engine)
        assert "PARENT_STARTED" in names
        assert runner.calls == [1]
        rt = RuntimeStore(Layout(tmp_repo)).load()
        assert rt.status == SupervisorStatus.STOPPED_SUCCESS
        assert rt.stop_reason == StopReason.TASK_COMPLETED
        assert rt.counters.parent_activations == 1
        # 运行目录完整
        rd = tmp_repo / ".supervisor" / "runs" / "activation-000001"
        assert (rd / "prompt.txt").exists()
        assert (rd / "stdout.log").exists()
        assert (rd / "result.json").exists()
        prompt = (rd / "prompt.txt").read_text(encoding="utf-8")
        assert "SUPERVISOR EVENT: INITIAL_START" in prompt
        # 初始 prompt 不带开发逻辑
        assert "如何修改代码" not in prompt

    def test_02_fresh_task_to_blocked(self, tmp_repo):
        cfg = fast_cfg()
        runner = FakeParentRunner([StepScript.blocked()], Layout(tmp_repo))
        engine = make_engine(tmp_repo, cfg, runner)
        rc = run_engine(engine)
        assert rc == 1
        rt = RuntimeStore(Layout(tmp_repo)).load()
        assert rt.status == SupervisorStatus.STOPPED_BLOCKED
        assert rt.stop_reason == StopReason.TASK_BLOCKED

    def test_16_invalid_initial_state_stops_error(self, tmp_repo):
        write_agent_state(tmp_repo, status="TESTING")  # Parent 内部语义
        runner = FakeParentRunner([StepScript.completed()], Layout(tmp_repo))
        engine = make_engine(tmp_repo, fast_cfg(), runner)
        rc = run_engine(engine)
        assert rc == 1
        assert runner.calls == []  # 不应启动任何 Parent
        rt = RuntimeStore(Layout(tmp_repo)).load()
        assert rt.status == SupervisorStatus.STOPPED_ERROR
        assert rt.stop_reason == StopReason.INVALID_AGENT_STATE
        assert "AGENT_STATE_INVALID" in event_names(engine)

    def test_16b_invalid_json_state_stops_error(self, tmp_repo):
        p = state_file(tmp_repo)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("{broken json", encoding="utf-8")
        runner = FakeParentRunner([StepScript.completed()], Layout(tmp_repo))
        engine = make_engine(tmp_repo, fast_cfg(), runner)
        rc = run_engine(engine)
        assert rc == 1
        assert runner.calls == []
        rt = RuntimeStore(Layout(tmp_repo)).load()
        assert rt.stop_reason == StopReason.INVALID_AGENT_STATE


# ---------------------------------------------------------- restart policy


class TestRestartPolicy:
    def test_03_clean_exit_running_new_activation(self, tmp_repo):
        cfg = fast_cfg()
        runner = FakeParentRunner(
            [StepScript.running(), StepScript.running(), StepScript.completed()],
            Layout(tmp_repo),
        )
        engine = make_engine(tmp_repo, cfg, runner)
        rc = run_engine(engine)
        assert rc == 0
        assert runner.calls == [1, 2, 3]
        rt = RuntimeStore(Layout(tmp_repo)).load()
        assert rt.status == SupervisorStatus.STOPPED_SUCCESS
        assert rt.counters.parent_activations == 3
        assert rt.counters.clean_restarts == 2
        assert "PARENT_CLEAN_EXIT_WITH_RUNNING_STATE" in event_names(engine)
        # 第二轮 prompt 是 CONTINUE
        p2 = (tmp_repo / ".supervisor" / "runs" / "activation-000002" / "prompt.txt").read_text(
            encoding="utf-8"
        )
        assert "SUPERVISOR EVENT: CONTINUE" in p2

    def test_04_crash_restart_with_recovery_prompt(self, tmp_repo):
        cfg = fast_cfg(max_crash_restarts=5)
        runner = FakeParentRunner(
            [StepScript.running(exit_code=1), StepScript.completed()],
            Layout(tmp_repo),
        )
        engine = make_engine(tmp_repo, cfg, runner)
        rc = run_engine(engine)
        assert rc == 0
        assert runner.calls == [1, 2]
        rt = RuntimeStore(Layout(tmp_repo)).load()
        assert rt.counters.crash_restarts == 1
        assert "PARENT_CRASH" in event_names(engine)
        p2 = (tmp_repo / ".supervisor" / "runs" / "activation-000002" / "prompt.txt").read_text(
            encoding="utf-8"
        )
        assert "RECOVER_AFTER_PARENT_CRASH" in p2
        assert "Do not restart the development task from scratch" in p2

    def test_17_missing_state_after_crash_restarts(self, tmp_repo):
        """exit!=0 + 状态文件缺失 → 按 crash 恢复，不给 traceback。"""
        cfg = fast_cfg(max_crash_restarts=5)
        runner = FakeParentRunner(
            [Step(status="RUNNING", exit_code=1, delete_state=True), StepScript.completed()],
            Layout(tmp_repo),
        )
        engine = make_engine(tmp_repo, cfg, runner)
        rc = run_engine(engine)
        assert rc == 0
        assert runner.calls == [1, 2]
        rt = RuntimeStore(Layout(tmp_repo)).load()
        assert rt.counters.crash_restarts == 1
        p2 = (tmp_repo / ".supervisor" / "runs" / "activation-000002" / "prompt.txt").read_text(
            encoding="utf-8"
        )
        assert "RECOVER_AFTER_PARENT_CRASH" in p2

    def test_18_stale_checkpoint_no_progress(self, tmp_repo):
        """exit0 + RUNNING + checkpoint_seq 未增 → 记录 no-progress。"""
        write_agent_state(tmp_repo, status="RUNNING", seq=1)  # 上一轮已写 checkpoint
        cfg = fast_cfg()
        runner = FakeParentRunner(
            [StepScript.running(stale=True), StepScript.completed()],
            Layout(tmp_repo),
        )
        engine = make_engine(tmp_repo, cfg, runner)
        rc = run_engine(engine)
        assert rc == 0
        assert runner.calls == [1, 2]
        assert "PARENT_NO_PROGRESS" in event_names(engine)

    def test_anomaly_exit1_with_fresh_wait_ci(self, tmp_repo):
        """exit1 + fresh WAIT_CI → 仍进 WAIT_CI，不重复启动 Parent（文档九）。"""
        cfg = fast_cfg()
        runner = FakeParentRunner([StepScript.wait_ci(exit_code=1)], Layout(tmp_repo))
        engine = make_engine(tmp_repo, cfg, runner)

        async def control(eng):
            await wait_until(lambda: "CI_DISABLED" in event_names(eng))
            await asyncio.sleep(0.2)
            eng.request_stop()

        rc = run_engine(engine, control)
        assert rc == 0
        assert "PARENT_EXITED" in event_names(engine)
        assert runner.calls == [1]  # 不重启
        exited = [e for e in EventLog(engine.layout.events_path).read_all() if e["event"] == "PARENT_EXITED"]
        assert exited[-1]["exit_code"] == 1


# ---------------------------------------------------------------- limits


class TestLimits:
    def test_05_repeated_crash_stop_limit(self, tmp_repo):
        cfg = fast_cfg(max_crash_restarts=3)
        runner = FakeParentRunner([StepScript.running(exit_code=1)], Layout(tmp_repo))
        engine = make_engine(tmp_repo, cfg, runner)
        rc = run_engine(engine)
        assert rc == 1
        assert runner.calls == [1, 2, 3]
        rt = RuntimeStore(Layout(tmp_repo)).load()
        assert rt.status == SupervisorStatus.STOPPED_LIMIT
        assert rt.stop_reason == StopReason.MAX_CRASH_RESTARTS
        assert rt.counters.crash_restarts == 3
        assert "LIMIT_REACHED" in event_names(engine)

    def test_07_repeated_timeout_stop_limit(self, tmp_repo):
        cfg = fast_cfg(max_timeouts=2)
        runner = FakeParentRunner(
            [Step(status="RUNNING", timed_out=True), Step(status="RUNNING", timed_out=True)],
            Layout(tmp_repo),
        )
        engine = make_engine(tmp_repo, cfg, runner)
        rc = run_engine(engine)
        assert rc == 1
        assert runner.calls == [1, 2]
        rt = RuntimeStore(Layout(tmp_repo)).load()
        assert rt.stop_reason == StopReason.MAX_TIMEOUTS
        assert rt.counters.timeouts == 2
        assert "PARENT_TIMEOUT" in event_names(engine)

    def test_08_max_activations_stop_limit(self, tmp_repo):
        cfg = fast_cfg(max_parent_activations=3)
        runner = FakeParentRunner([StepScript.running()], Layout(tmp_repo))
        engine = make_engine(tmp_repo, cfg, runner)
        rc = run_engine(engine)
        assert rc == 1
        assert runner.calls == [1, 2, 3]
        rt = RuntimeStore(Layout(tmp_repo)).load()
        assert rt.stop_reason == StopReason.MAX_PARENT_ACTIVATIONS
        assert rt.counters.parent_activations == 3

    def test_09_max_wall_time_stop_limit(self, tmp_repo):
        cfg = fast_cfg(
            max_active_wall_seconds=1,
            max_clean_restarts=1000,
            max_parent_activations=1000,
        )
        runner = FakeParentRunner([StepScript.running(delay=0.05)], Layout(tmp_repo))
        engine = make_engine(tmp_repo, cfg, runner)
        rc = run_engine(engine)
        assert rc == 1
        rt = RuntimeStore(Layout(tmp_repo)).load()
        assert rt.stop_reason == StopReason.MAX_ACTIVE_WALL_TIME
        assert rt.counters.parent_activations >= 2
        assert len(runner.calls) < 100  # 不会无限跑

    def test_06_timeout_then_restart(self, tmp_repo):
        cfg = fast_cfg(max_timeouts=5)
        runner = FakeParentRunner(
            [Step(status="RUNNING", timed_out=True), StepScript.completed()],
            Layout(tmp_repo),
        )
        engine = make_engine(tmp_repo, cfg, runner)
        rc = run_engine(engine)
        assert rc == 0
        assert runner.calls == [1, 2]
        rt = RuntimeStore(Layout(tmp_repo)).load()
        assert rt.counters.timeouts == 1
        p2 = (tmp_repo / ".supervisor" / "runs" / "activation-000002" / "prompt.txt").read_text(
            encoding="utf-8"
        )
        assert "RECOVER_AFTER_PARENT_TIMEOUT" in p2

    def test_06b_real_timeout_via_dsh_runner(self, tmp_repo, monkeypatch):
        """真实子进程：engine + DshRunner + fake dsh 挂起 → 超时击杀。"""
        monkeypatch.setenv("FAKE_DSH_MODE", "hang")
        monkeypatch.setenv(
            "FAKE_DSH_STATE",
            json.dumps(
                {
                    "schema_version": 1,
                    "status": "RUNNING",
                    "checkpoint_seq": 1,
                    "updated_at": "2026-08-18T06:00:00Z",
                }
            ),
        )
        cfg = fast_cfg(max_timeouts=2, parent_timeout_seconds=1, terminate_grace_seconds=1)
        runner = DshRunner(
            executable=str(FAKE_DSH), profile="headless", terminate_grace_seconds=1
        )
        engine = make_engine(tmp_repo, cfg, runner)
        rc = run_engine(engine)
        assert rc == 1
        rt = RuntimeStore(Layout(tmp_repo)).load()
        assert rt.stop_reason == StopReason.MAX_TIMEOUTS
        assert rt.counters.timeouts == 2
        starts = [
            e
            for e in EventLog(engine.layout.events_path).read_all()
            if e["event"] == "PARENT_STARTED"
        ]
        assert len(starts) == 2
        # 超时后进程组必须清干净
        time.sleep(0.2)


# ------------------------------------------------------------- wait states


class TestWaitStates:
    def test_10_wait_ci_no_parent_starts(self, tmp_repo):
        cfg = fast_cfg()
        cfg.ci.enabled = False
        runner = FakeParentRunner([StepScript.wait_ci()], Layout(tmp_repo))
        engine = make_engine(tmp_repo, cfg, runner)

        async def control(eng):
            await wait_until(lambda: "CI_DISABLED" in event_names(eng))
            await asyncio.sleep(0.3)
            starts = [e for e in EventLog(eng.layout.events_path).read_all() if e["event"] == "PARENT_STARTED"]
            assert len(starts) == 1  # 等待期间绝不启动第二个 Parent
            eng.request_stop()

        rc = run_engine(engine, control)
        assert rc == 0
        assert runner.calls == [1]
        rt = RuntimeStore(Layout(tmp_repo)).load()
        assert rt.status == SupervisorStatus.STOPPED_OPERATOR

    def test_15_wait_human_no_parent_then_resume(self, tmp_repo):
        cfg = fast_cfg()
        runner = FakeParentRunner(
            [StepScript.wait_human(), StepScript.completed()],
            Layout(tmp_repo),
        )
        engine = make_engine(tmp_repo, cfg, runner)

        async def control(eng):
            await wait_until(lambda: "WAIT_HUMAN" in event_names(eng))
            await asyncio.sleep(0.2)
            starts_before = [
                e for e in EventLog(eng.layout.events_path).read_all() if e["event"] == "PARENT_STARTED"
            ]
            assert len(starts_before) == 1
            # 人工通过 `supervisor resume --event HUMAN_APPROVED` 续跑
            atomic_write_json(eng.layout.resume_path, {"event": "HUMAN_APPROVED"})

        rc = run_engine(engine, control)
        assert rc == 0
        assert runner.calls == [1, 2]
        rt = RuntimeStore(Layout(tmp_repo)).load()
        assert rt.status == SupervisorStatus.STOPPED_SUCCESS
        assert rt.stop_reason == StopReason.TASK_COMPLETED
        p2 = (tmp_repo / ".supervisor" / "runs" / "activation-000002" / "prompt.txt").read_text(
            encoding="utf-8"
        )
        assert "HUMAN_APPROVED" in p2
        assert not engine.layout.resume_path.exists()  # resume 标记被消费


# --------------------------------------------------------------- recovery


class TestRecovery:
    def test_19_restart_with_dead_parent_continues(self, tmp_repo):
        """Supervisor 重启：记录的 Parent 已死 → 恢复正常继续。"""
        write_runtime(tmp_repo, activation=5, pid=999999, start_id="dead-start")
        write_agent_state(tmp_repo, status="RUNNING", seq=5)
        runner = FakeParentRunner([StepScript.completed()], Layout(tmp_repo))
        engine = make_engine(tmp_repo, fast_cfg(), runner)
        rc = run_engine(engine)
        assert rc == 0
        assert runner.calls == [6]  # 从记录的下一个激活继续，不重复
        names = event_names(engine)
        assert "SUPERVISOR_CRASH_RECOVERY" in names
        rt = RuntimeStore(Layout(tmp_repo)).load()
        assert rt.counters.parent_activations == 6

    def test_20_restart_with_live_parent_adopts_orphan(self, tmp_repo):
        """Supervisor 重启：原 Parent 还活着 → 收养，绝不启动第二个。"""
        env = dict(os.environ, FAKE_DSH_MODE="hang")
        orphan = subprocess.Popen(
            [sys.executable, str(FAKE_DSH), "--profile", "headless", "orphan-task"],
            cwd=str(tmp_repo),
            env=env,
            start_new_session=True,
        )
        for _ in range(20):
            start_id = read_start_id(orphan.pid)
            if start_id is not None:
                try:
                    if Path(f"/proc/{orphan.pid}/cmdline").read_bytes():
                        break
                except OSError:
                    pass
            time.sleep(0.05)
        else:
            start_id = read_start_id(orphan.pid)
        write_runtime(
            tmp_repo, activation=7, pid=orphan.pid, start_id=start_id,
        )
        write_agent_state(tmp_repo, status="RUNNING", seq=7)
        runner = FakeParentRunner([StepScript.completed()], Layout(tmp_repo))
        engine = make_engine(tmp_repo, fast_cfg(), runner)

        async def control(eng):
            await wait_until(lambda: "ORPHAN_ADOPTED" in event_names(eng))
            await asyncio.sleep(0.3)
            starts = [
                e for e in EventLog(eng.layout.events_path).read_all() if e["event"] == "PARENT_STARTED"
            ]
            assert starts == []  # 收养期间不启动新 Parent
            os.killpg(orphan.pid, signal.SIGKILL)
            orphan.wait()  # reap 僵尸，/proc/<pid> 消失后引擎才能察觉
            await wait_until(
                lambda: any(e["event"] == "PARENT_STARTED" for e in EventLog(eng.layout.events_path).read_all())
            )

        rc = run_engine(engine, control)
        assert rc == 0
        assert runner.calls == [8]
        assert "ORPHAN_EXITED" in event_names(engine)

    def test_21_second_supervisor_rejected_by_lock(self, tmp_repo):
        cfg = fast_cfg()
        runner_a = FakeParentRunner([StepScript.wait_human()], Layout(tmp_repo))
        engine_a = make_engine(tmp_repo, cfg, runner_a)

        async def control(eng_a):
            await wait_until(lambda: "WAIT_HUMAN" in event_names(eng_a))
            # 第二个 Supervisor 尝试启动同仓库 → 必须被锁拒绝
            runner_b = FakeParentRunner([StepScript.completed()], Layout(tmp_repo))
            engine_b = make_engine(tmp_repo, fast_cfg(), runner_b)
            with pytest.raises(LockHeldError):
                await engine_b.run_forever()
            assert runner_b.calls == []  # 绝不能启动任何 Parent
            eng_a.request_stop()

        rc = run_engine(engine_a, control)
        assert rc == 0

    def test_22_operator_stop_while_waiting(self, tmp_repo):
        cfg = fast_cfg()
        runner = FakeParentRunner([StepScript.wait_human()], Layout(tmp_repo))
        engine = make_engine(tmp_repo, cfg, runner)

        async def control(eng):
            await wait_until(lambda: "WAIT_HUMAN" in event_names(eng))
            eng.request_stop()

        rc = run_engine(engine, control)
        assert rc == 0
        rt = RuntimeStore(Layout(tmp_repo)).load()
        assert rt.status == SupervisorStatus.STOPPED_OPERATOR
        assert rt.stop_reason == StopReason.OPERATOR_STOP