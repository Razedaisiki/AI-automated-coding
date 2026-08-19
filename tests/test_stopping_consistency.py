"""M5 hardening 第三轮评审回归测试（crash consistency 收尾）。

评审要点：
- P0-A1  STOPPING 必须**持久化地**保持，直到 Parent 组真正消失 → STOPPED_OPERATOR；
         恢复启动时不得落盘成 BOOTING（否则二次崩溃丢失 stop intent）。
- P0-A2  STOPPING + pid=None + process.json 尚未生成 + lease held：
         stop 收尾必须像普通 reconciliation 一样等待 record/lease，
         绝不能直接结束 stop 把旧 launcher 留下的 Parent 丢在后台。
- P0-A3  STARTING reconciliation 在等 lease 时收到 stop → 切入 STOPPING，
         不得落入 PARENT_SPAWN_UNCONFIRMED / 清 current_parent。
- P1-B1  terminate_process_group 若最终 PGID 仍 alive → 显式失败
         （返回 False/抛错），Supervisor 绝不写 STOPPED_OPERATOR。
- P1-B2  PGID 击杀必须身份可验证：start_id 缺失/不符 → 不按裸 pid 杀。
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

from supervisor.config import default_config
from supervisor.engine import SupervisorEngine
from supervisor.events import EventLog
from supervisor.lock import ParentLease
from supervisor.models import StopReason, SupervisorStatus
from supervisor.process_identity import is_proc_alive, process_group_alive, read_start_id
from supervisor.storage import Layout, RuntimeStore, atomic_write_json

from conftest import FAKE_DSH, FakeParentRunner, StepScript, event_names, run_engine, wait_until

from test_hardening import _runtime_dict, _spawn_orphan, _write_agent_state, _write_runtime


def _iso_now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _counter_dict(activations=4):
    return {
        "parent_activations": activations,
        "crash_restarts": 0,
        "clean_restarts": 0,
        "timeouts": 0,
        "ci_wakeups": 0,
    }


class _HoldingLease:
    """占住 parent.lock 的进程（模拟还活着的旧 launcher/DSH）。"""

    def __init__(self, repo):
        self.repo = repo
        code = (
            "import fcntl,os,sys,time\n"
            "fd=os.open(sys.argv[1], os.O_CREAT|os.O_RDWR, 0o644)\n"
            "fcntl.flock(fd, fcntl.LOCK_EX)\n"
            "print('held', flush=True)\n"
            "while True: time.sleep(60)\n"
        )
        self.proc = subprocess.Popen(
            [sys.executable, "-c", code, str(repo / ".supervisor" / "parent.lock")],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        self.proc.stdout.readline()

    def release(self):
        if self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.release()


def _write_process_record(repo, activation_id, pid, start_id, token):
    run_dir = Layout(repo).run_dir(activation_id)
    run_dir.mkdir(parents=True, exist_ok=True)
    atomic_write_json(
        run_dir / "process.json",
        {
            "pid": pid,
            "process_start_id": start_id,
            "activation_id": activation_id,
            "activation_token": token,
            "written_at": _iso_now(),
        },
    )


class TestStoppingDurable:
    def test_stopping_status_survives_restore_on_disk(self, tmp_repo):
        """P0-A1：恢复 STOPPING 时磁盘必须**继续保持 STOPPING**（不能落盘成 BOOTING）。"""
        _write_runtime(
            tmp_repo,
            _runtime_dict(tmp_repo, status="STOPPING", current_parent=None),
        )
        runner = FakeParentRunner([], Layout(tmp_repo))
        engine = SupervisorEngine(base_dir=tmp_repo, config=default_config(), runner=runner)
        rt = engine._restore_or_init_runtime()
        assert rt.status == SupervisorStatus.STOPPING
        assert RuntimeStore(Layout(tmp_repo)).load().status == SupervisorStatus.STOPPING


class TestStoppingLeaseReconciliation:
    def test_stopping_pid_unknown_waits_for_record_then_kills(self, tmp_repo):
        """P0-A2：STOPPING + pid=None + record 稍后才出现 + lease held：
        必须先等 record/lease，找到可信 pid 杀组后才 STOPPED_OPERATOR；
        绝不等不到就“完成 stop”把 Parent 丢在后台。"""
        orphan, start_id = _spawn_orphan(tmp_repo)  # 模拟"旧 launcher 稍后 exec 出来的 DSH"
        token = "tok-stop-wait"
        _write_runtime(
            tmp_repo,
            _runtime_dict(
                tmp_repo,
                status="STOPPING",
                current_parent={
                    "activation_id": 3,
                    "activation_token": token,
                    "pid": None,
                    "process_start_id": None,
                    "started_at": _iso_now(),
                    "reason": "CONTINUE",
                },
                counters=_counter_dict(activations=2),
            ),
        )
        _write_agent_state(tmp_repo, seq=3)
        with _HoldingLease(tmp_repo) as holder:
            cfg = default_config()
            cfg.limits.terminate_grace_seconds = 1
            runner = FakeParentRunner([StepScript.completed()], Layout(tmp_repo))
            engine = SupervisorEngine(base_dir=tmp_repo, config=cfg, runner=runner)

            async def control(eng):
                # 引擎进入 stop 收尾的 lease 等待
                await wait_until(lambda: "PARENT_LEASE_HELD" in event_names(eng))
                await asyncio.sleep(0.3)
                # 此刻绝不能已经“完成 stop”（Parent 是活的）
                rt = RuntimeStore(Layout(eng.base)).load()
                assert rt.status != SupervisorStatus.STOPPED_OPERATOR
                # 旧 launcher 姗姗来迟写下 process.json
                _write_process_record(tmp_repo, 3, orphan.pid, start_id, token)
                # 引擎应找到可信 pid → 杀组 → 完成 stop
                await wait_until(
                    lambda: RuntimeStore(Layout(eng.base)).load().status
                    == SupervisorStatus.STOPPED_OPERATOR
                )

            rc = run_engine(engine, control)
            assert rc == 0
            assert runner.calls == []  # 绝不 spawn
            rt = RuntimeStore(Layout(tmp_repo)).load()
            assert rt.status == SupervisorStatus.STOPPED_OPERATOR
            assert rt.stop_reason == StopReason.OPERATOR_STOP
            try:
                orphan.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
            assert not process_group_alive(orphan.pid), "orphan left running after stop"

    def test_reconcile_stop_cutin_never_emits_spawn_unconfirmed(self, tmp_repo):
        """P0-A3：STARTING reconciliation 等 lease 时收到 operator stop →
        切入 STOPPING 并完成收尾；绝不落入 PARENT_SPAWN_UNCONFIRMED / 清 current_parent。"""
        token = "tok-stop-cutin"
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
                counters=_counter_dict(activations=2),
            ),
        )
        _write_agent_state(tmp_repo, seq=1)
        with _HoldingLease(tmp_repo) as holder:
            cfg = default_config()
            cfg.limits.terminate_grace_seconds = 0.5
            runner = FakeParentRunner([StepScript.completed()], Layout(tmp_repo))
            engine = SupervisorEngine(base_dir=tmp_repo, config=cfg, runner=runner)

            async def control(eng):
                await wait_until(lambda: "PARENT_LEASE_HELD" in event_names(eng))
                await asyncio.sleep(0.2)
                eng.request_stop()
                # 必须切入 STOPPING（不能当 spawn-unconfirmed 处理）
                await wait_until(
                    lambda: RuntimeStore(Layout(eng.base)).load().status
                    == SupervisorStatus.STOPPING
                )
                await asyncio.sleep(0.3)
                assert "PARENT_SPAWN_UNCONFIRMED" not in event_names(eng)
                assert not EventLog(eng.layout.events_path).events_named("PARENT_STARTED")
                # 释放 lease → stop 收尾完成
                holder.release()

            rc = run_engine(engine, control)
            assert rc == 0
            assert runner.calls == []
            rt = RuntimeStore(Layout(tmp_repo)).load()
            assert rt.status == SupervisorStatus.STOPPED_OPERATOR
            assert rt.stop_reason == StopReason.OPERATOR_STOP


class TestTerminationFailsClosed:
    def test_operator_stop_with_surviving_group_errors(self, tmp_repo, monkeypatch):
        """P1-B1：终止后 PGID 仍 alive → terminate_process_group 返回 False →
        Supervisor 绝不以 STOPPED_OPERATOR 收场，改为 STOPPED_ERROR。"""
        import supervisor.dsh_runner as dsh_runner

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
                counters=_counter_dict(activations=4),
            ),
        )
        _write_agent_state(tmp_repo, seq=5)
        cfg = default_config()
        cfg.limits.terminate_grace_seconds = 0.5

        async def fake_terminate(pid, grace):
            return False  # 模拟"进程组熬过了 SIGKILL"

        monkeypatch.setattr(dsh_runner, "terminate_process_group", fake_terminate)
        runner = FakeParentRunner([], Layout(tmp_repo))
        engine = SupervisorEngine(base_dir=tmp_repo, config=cfg, runner=runner)

        async def control(eng):
            await wait_until(lambda: "ORPHAN_ADOPTED" in event_names(eng))
            await asyncio.sleep(0.2)
            eng.request_stop()

        rc = run_engine(engine, control)
        assert rc == 1
        rt = RuntimeStore(Layout(tmp_repo)).load()
        assert rt.status == SupervisorStatus.STOPPED_ERROR
        assert rt.stop_reason == StopReason.SUPERVISOR_INTERNAL_ERROR
        assert "SUPERVISOR_STOPPED" in event_names(engine)  # 有终态事件
        # 清理：真实进程组由测试兜底杀掉
        try:
            os.killpg(orphan.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        try:
            orphan.wait(timeout=3)
        except subprocess.TimeoutExpired:
            pass

    def test_stop_cancel_verifies_group_gone(self, tmp_repo, monkeypatch):
        """P1-B1：operator stop 取消激活后，引擎必须确认进程组已消失；
        仍 alive → STOPPED_ERROR，绝不写 STOPPED_OPERATOR。"""
        from supervisor.dsh_runner import DshRunner

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
        runner = DshRunner(executable=str(FAKE_DSH), profile="headless", terminate_grace_seconds=1)

        async def fake_terminate(proc):  # 模拟终止完全没生效
            return False

        monkeypatch.setattr(runner, "_terminate_group", fake_terminate)
        cfg = default_config()
        cfg.limits.terminate_grace_seconds = 1
        engine = SupervisorEngine(base_dir=tmp_repo, config=cfg, runner=runner)
        real_pid = {"pid": None}

        async def control(eng):
            await wait_until(
                lambda: bool(EventLog(eng.layout.events_path).events_named("PARENT_STARTED"))
            )
            await asyncio.sleep(0.3)
            rt = RuntimeStore(Layout(eng.base)).load()
            real_pid["pid"] = rt.current_parent.pid if rt.current_parent else None
            eng.request_stop()

        rc = run_engine(engine, control)
        assert rc == 1
        rt = RuntimeStore(Layout(tmp_repo)).load()
        assert rt.status == SupervisorStatus.STOPPED_ERROR
        assert rt.stop_reason == StopReason.SUPERVISOR_INTERNAL_ERROR
        # 清理真实子进程组
        if real_pid["pid"]:
            try:
                os.killpg(real_pid["pid"], signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass


class TestIdentityGuardedKill:
    def test_clean_recorded_group_requires_start_id(self, tmp_repo):
        """P1-B2：start_id 缺失时 `_clean_recorded_group` 绝不按裸 pid 杀组。"""
        orphan, start_id = _spawn_orphan(tmp_repo)
        runner = FakeParentRunner([], Layout(tmp_repo))
        engine = SupervisorEngine(base_dir=tmp_repo, config=default_config(), runner=runner)

        asyncio.run(engine._clean_recorded_group(orphan.pid, None))
        assert is_proc_alive(orphan.pid), "group killed without verifiable leader identity"
        # 清理
        try:
            os.killpg(orphan.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        try:
            orphan.wait(timeout=3)
        except subprocess.TimeoutExpired:
            pass