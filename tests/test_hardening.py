"""M5 hardening 回归测试（评审 P0/P1 复现问题）。"""

import asyncio
import json
import os
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from supervisor.config import default_config
from supervisor.engine import SupervisorEngine
from supervisor.events import EventLog
from supervisor.models import (
    AgentState,
    StopReason,
    SupervisorStatus,
)
from supervisor.process_identity import read_start_id
from supervisor.storage import Layout, RuntimeStore, atomic_write_json

from conftest import FAKE_DSH, FakeParentRunner, StepScript, event_names, run_engine, wait_until


def _iso_now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _runtime_dict(repo, **overrides):
    """构造一份完整合法的 runtime.json dict（绕过模型，便于构造旧/残缺状态）。"""
    cfg = default_config()
    L = cfg.limits
    data = {
        "schema_version": 1,
        "status": "RUNNING_PARENT",
        "task_started_at": _iso_now(),
        "current_parent": None,
        "counters": {
            "parent_activations": 0,
            "crash_restarts": 0,
            "clean_restarts": 0,
            "timeouts": 0,
            "ci_wakeups": 0,
        },
        "limits": {
            "max_parent_activations": L.max_parent_activations,
            "max_crash_restarts": L.max_crash_restarts,
            "max_clean_restarts": L.max_clean_restarts,
            "max_timeouts": L.max_timeouts,
            "max_ci_wakeups": L.max_ci_wakeups,
            "max_active_wall_seconds": L.max_active_wall_seconds,
            "parent_timeout_seconds": L.parent_timeout_seconds,
            "terminate_grace_seconds": L.terminate_grace_seconds,
        },
        "last_agent_checkpoint_seq": 0,
        "supervisor_pid": None,
        "active_budget": {"accrued_seconds": 0.0, "last_mark": None},
        "stop_reason": None,
    }
    data.update(overrides)
    return data


def _write_runtime(repo, data):
    atomic_write_json(repo / ".supervisor" / "runtime.json", data)


def _write_agent_state(repo, seq=3, status="RUNNING"):
    raw = {
        "schema_version": 1,
        "status": status,
        "checkpoint_seq": seq,
        "updated_at": _iso_now(),
    }
    (repo / ".agent").mkdir(parents=True, exist_ok=True)
    atomic_write_json(repo / ".agent" / "state.json", raw)


def _spawn_orphan(repo):
    """真实挂起的 fake dsh 进程（即"还活着的旧 Parent"）。"""
    env = dict(os.environ, FAKE_DSH_MODE="hang")
    proc = subprocess.Popen(
        [sys.executable, str(FAKE_DSH), "orphan-task"],
        cwd=str(repo),
        env=env,
        start_new_session=True,
    )
    # 等待 /proc 就绪且 cmdline 可读，避免竞态导致 start_id/cmdline 为空
    for _ in range(20):
        sid = read_start_id(proc.pid)
        if sid is not None:
            # 再确认 cmdline 已写入
            try:
                if (Path(f"/proc/{proc.pid}/cmdline").read_bytes()):
                    return proc, sid
            except OSError:
                pass
        time.sleep(0.05)
    return proc, read_start_id(proc.pid)


def _pgid_alive(pid):
    from supervisor.process_identity import is_proc_alive

    return is_proc_alive(pid)


# ---------------------------------------------------------------- P0 #1


class TestAdoptionStopKillsOrphan:
    def test_operator_stop_during_adoption_kills_orphan(self, tmp_repo):
        """收养孤儿时收到 operator stop → 必须 SIGTERM→SIGKILL 整个进程组，
        不得把 Parent 留在后台，也不得误记 ORPHAN_EXITED。"""
        orphan, start_id = _spawn_orphan(tmp_repo)
        _write_runtime(
            tmp_repo,
            _runtime_dict(
                tmp_repo,
                current_parent={
                    "activation_id": 5,
                    "pid": orphan.pid,
                    "process_start_id": start_id,
                    "started_at": _iso_now(),
                    "reason": "CONTINUE",
                },
                counters={"parent_activations": 4, "crash_restarts": 0,
                          "clean_restarts": 0, "timeouts": 0, "ci_wakeups": 0},
            ),
        )
        _write_agent_state(tmp_repo, seq=5)
        cfg = default_config()
        cfg.limits.terminate_grace_seconds = 1
        runner = FakeParentRunner([], Layout(tmp_repo))
        engine = SupervisorEngine(base_dir=tmp_repo, config=cfg, runner=runner)

        async def control(eng):
            await wait_until(lambda: "ORPHAN_ADOPTED" in event_names(eng))
            await asyncio.sleep(0.1)
            eng.request_stop()

        rc = run_engine(engine, control)
        assert rc == 0
        rt = RuntimeStore(Layout(tmp_repo)).load()
        assert rt.status == SupervisorStatus.STOPPED_OPERATOR
        assert rt.stop_reason == StopReason.OPERATOR_STOP
        assert rt.current_parent is None
        # 收养期被杀的孤儿会先成僵尸，需 reap 后 /proc 消失
        try:
            orphan.wait(timeout=2)
        except subprocess.TimeoutExpired:
            pass
        assert not _pgid_alive(orphan.pid), "orphan survived operator stop"
        names = event_names(engine)
        assert "PARENT_KILLED" in names
        assert "ORPHAN_EXITED" not in names  # 不是它自己退出的


# ---------------------------------------------------------------- P0 #2


class TestStartingParentReconcile:
    def test_spawn_window_record_present_adopts(self, tmp_repo):
        """崩溃窗口状态：runtime 只有 STARTING_PARENT + token（pid 未知），
        但子进程通过 process.json 自留了身份 → 必须收养，不得重复启动。"""
        orphan, start_id = _spawn_orphan(tmp_repo)
        token = "tok-9e9b"
        run_dir = Layout(tmp_repo).run_dir(3)
        run_dir.mkdir(parents=True)
        atomic_write_json(
            run_dir / "process.json",
            {
                "pid": orphan.pid,
                "process_start_id": start_id,
                "activation_id": 3,
                "activation_token": token,
                "written_at": _iso_now(),
            },
        )
        _write_runtime(
            tmp_repo,
            _runtime_dict(
                tmp_repo,
                status="STARTING_PARENT",
                current_parent={
                    "activation_id": 3,
                    "activation_token": token,
                    "pid": None,
                    "process_start_id": None,
                    "started_at": _iso_now(),
                    "reason": "INITIAL_START",
                },
                counters={"parent_activations": 2, "crash_restarts": 0,
                          "clean_restarts": 0, "timeouts": 0, "ci_wakeups": 0},
            ),
        )
        _write_agent_state(tmp_repo, seq=3)
        runner = FakeParentRunner([StepScript.completed()], Layout(tmp_repo))
        engine = SupervisorEngine(
            base_dir=tmp_repo, config=default_config(), runner=runner
        )

        async def control(eng):
            await wait_until(lambda: "ORPHAN_ADOPTED" in event_names(eng))
            await asyncio.sleep(0.3)
            starts = EventLog(eng.layout.events_path).events_named("PARENT_STARTED")
            assert len(starts) == 0  # 收养期间绝不重复启动
            os.killpg(orphan.pid, signal.SIGKILL)
            orphan.wait()
            await wait_until(
                lambda: any(
                    e["event"] == "PARENT_STARTED"
                    for e in EventLog(eng.layout.events_path).read_all()
                )
            )

        rc = run_engine(engine, control)
        assert rc == 0
        assert runner.calls == [4]  # 激活号不重复
        assert "PARENT_RECONCILED" in event_names(engine)

    def test_spawn_window_no_record_respawns(self, tmp_repo):
        """崩溃窗口状态：没有 process.json 记录 → 有界宽限后允许重新 spawn。"""
        token = "tok-dead"
        _write_runtime(
            tmp_repo,
            _runtime_dict(
                tmp_repo,
                status="STARTING_PARENT",
                current_parent={
                    "activation_id": 3,
                    "activation_token": token,
                    "pid": None,
                    "process_start_id": None,
                    "started_at": _iso_now(),
                    "reason": "INITIAL_START",
                },
                counters={"parent_activations": 2, "crash_restarts": 0,
                          "clean_restarts": 0, "timeouts": 0, "ci_wakeups": 0},
            ),
        )
        _write_agent_state(tmp_repo, seq=3)
        runner = FakeParentRunner([StepScript.completed()], Layout(tmp_repo))
        engine = SupervisorEngine(
            base_dir=tmp_repo, config=default_config(), runner=runner
        )
        rc = run_engine(engine)
        assert rc == 0
        assert runner.calls == [3]  # 重新 spawn，但激活号一致
        assert "PARENT_SPAWN_UNCONFIRMED" in event_names(engine)

    def test_starting_event_precedes_started(self, tmp_repo):
        """审计语义：PARENT_STARTING（意图）先于 PARENT_STARTED（确认 PID）。"""
        runner = FakeParentRunner([StepScript.completed()], Layout(tmp_repo))
        engine = SupervisorEngine(base_dir=tmp_repo, config=default_config(), runner=runner)
        rc = run_engine(engine)
        assert rc == 0
        rows = EventLog(engine.layout.events_path).read_all()
        starting = [i for i, r in enumerate(rows) if r["event"] == "PARENT_STARTING"]
        started = [i for i, r in enumerate(rows) if r["event"] == "PARENT_STARTED"]
        assert len(starting) == 1 and len(started) == 1
        assert starting[0] < started[0]
        assert "pid" in rows[started[0]]


# ------------------------------------------------------------- P0/P1 #3


class TestAdoptionEnforcesLimits:
    def test_adoption_enforces_parent_timeout(self, tmp_repo):
        """收养孤儿期间 parent timeout 必须继续生效：超时 → 杀进程组 → 计数。"""
        orphan, start_id = _spawn_orphan(tmp_repo)
        _write_runtime(
            tmp_repo,
            _runtime_dict(
                tmp_repo,
                current_parent={
                    "activation_id": 5,
                    "pid": orphan.pid,
                    "process_start_id": start_id,
                    "started_at": _iso_now(),
                    "reason": "CONTINUE",
                },
                counters={"parent_activations": 4, "crash_restarts": 0,
                          "clean_restarts": 0, "timeouts": 0, "ci_wakeups": 0},
            ),
        )
        _write_agent_state(tmp_repo, seq=5)
        cfg = default_config()
        cfg.limits.parent_timeout_seconds = 1
        cfg.limits.terminate_grace_seconds = 1
        cfg.limits.max_timeouts = 1
        cfg.restart.backoff_seconds = [0.01]
        runner = FakeParentRunner([], Layout(tmp_repo))
        engine = SupervisorEngine(base_dir=tmp_repo, config=cfg, runner=runner)

        t0 = time.monotonic()
        rc = run_engine(engine)
        elapsed = time.monotonic() - t0
        assert elapsed < 8.0
        assert rc == 1
        rt = RuntimeStore(Layout(tmp_repo)).load()
        assert rt.stop_reason == StopReason.MAX_TIMEOUTS
        assert rt.counters.timeouts >= 1
        names = event_names(engine)
        assert "PARENT_TIMEOUT" in names
        assert "PARENT_KILLED" in names
        try:
            orphan.wait(timeout=2)
        except subprocess.TimeoutExpired:
            pass
        assert not _pgid_alive(orphan.pid), "timed-out orphan survived"


# ----------------------------------------------------------------- P1 #4


class TestStopIdentity:
    def _write_supervisor_runtime(self, repo, pid, sid):
        _write_runtime(
            repo,
            _runtime_dict(repo, supervisor_pid=pid),
        )
        # 直接注入 process_start_id（旧 runtime 可能没有该字段）
        data = json.loads((repo / ".supervisor" / "runtime.json").read_text(encoding="utf-8"))
        if sid is None:
            data.pop("supervisor_process_start_id", None)
        else:
            data["supervisor_process_start_id"] = sid
        _write_runtime(repo, data)

    def test_stop_rejects_wrong_supervisor_identity(self, tmp_repo):
        """runtime 里的 supervisor_pid 被系统复用给无关进程 → stop 不得发信号。"""
        dummy = subprocess.Popen(["sleep", "60"])
        try:
            self._write_supervisor_runtime(tmp_repo, dummy.pid, "wrong-start-id")
            from supervisor.cli import main

            rc = main(["stop", str(tmp_repo)])
            assert rc == 1  # 身份不匹配 → 拒绝
            time.sleep(0.2)
            assert _pgid_alive(dummy.pid), "dummy process was SIGTERMed by mistake"
        finally:
            if dummy.poll() is None:
                dummy.kill()
                dummy.wait()

    def test_stop_missing_identity_refuses(self, tmp_repo):
        """旧 runtime 没有 supervisor_process_start_id → 不作为真 supervisor。"""
        dummy = subprocess.Popen(["sleep", "60"])
        try:
            self._write_supervisor_runtime(tmp_repo, dummy.pid, None)
            from supervisor.cli import main

            rc = main(["stop", str(tmp_repo)])
            assert rc == 1
            time.sleep(0.2)
            assert _pgid_alive(dummy.pid)
        finally:
            if dummy.poll() is None:
                dummy.kill()
                dummy.wait()