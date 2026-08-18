"""对抗式审查补充测试：文档核心主张的端到端回归。"""

import json
import os
import signal
import subprocess
import sys
import time

from supervisor.config import default_config
from supervisor.events import EventLog
from supervisor.models import RuntimeState, StopReason, SupervisorStatus
from supervisor.storage import Layout, RuntimeStore

from conftest import PROJECT_DIR, write_repo_toml


def _state_json(status="RUNNING", seq=1):
    return json.dumps(
        {
            "schema_version": 1,
            "status": status,
            "checkpoint_seq": seq,
            "updated_at": "2026-08-18T06:00:00Z",
        }
    )


def _wait_for(predicate, timeout=15.0, step=0.05):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(step)
    return False


class TestKillParentExternally:
    def test_supervisor_recovers_after_external_parent_kill(self, tmp_repo):
        """文档核心主张：随时杀掉一个 Parent（外部 SIGKILL），Supervisor 检测到
        crash 并恢复（记录 crash 事件），进程组被清干净。"""
        write_repo_toml(tmp_repo, default_config(), fake_dsh=True)
        layout = Layout(tmp_repo)
        env = dict(os.environ, FAKE_DSH_MODE="hang", FAKE_DSH_STATE=_state_json("RUNNING"))
        proc = subprocess.Popen(
            [sys.executable, "-m", "supervisor", "run", str(tmp_repo)],
            cwd=str(PROJECT_DIR),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        parent_pid = None
        try:
            # 等 Parent 启动并记录 pid
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                rt = RuntimeStore(layout).load()
                if rt is not None and rt.current_parent and rt.current_parent.pid:
                    parent_pid = rt.current_parent.pid
                    break
                time.sleep(0.05)
            assert parent_pid is not None, "supervisor never started a parent"
            assert os.path.exists(f"/proc/{parent_pid}")

            # 外部直接 SIGKILL 整个 Parent 进程组
            os.killpg(parent_pid, signal.SIGKILL)

            # Supervisor 必须察觉到 crash（不依赖 stdin/stdout，只靠状态机）
            assert _wait_for(
                lambda: any(
                    e["event"] == "PARENT_CRASH"
                    for e in EventLog(layout.events_path).read_all()
                )
            ), "external parent kill was not detected as a crash"

            rt = RuntimeStore(layout).load()
            assert rt.counters.crash_restarts >= 1

            # 收尾：干净停止 supervisor
            proc.terminate()
            proc.wait(timeout=10)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
            alive = False
            if parent_pid:
                try:
                    os.kill(parent_pid, 0)
                    alive = True
                except (ProcessLookupError, OSError):
                    alive = False
                if alive:  # 进程组还残留则强杀（不应发生）
                    os.killpg(parent_pid, signal.SIGKILL)

        assert not alive, "killed parent process group still alive"
        rt = RuntimeStore(layout).load()
        assert rt.status == SupervisorStatus.STOPPED_OPERATOR


class TestSecondSupervisorRejectedCli:
    def test_second_supervisor_run_exit_code_2(self, tmp_repo):
        """操作员层面：第一个 supervisor 运行中，第二个 `run` 必须被锁拒绝，rc=2。"""
        write_repo_toml(tmp_repo, default_config(), fake_dsh=True)
        env = dict(os.environ, FAKE_DSH_MODE="hang", FAKE_DSH_STATE=_state_json("RUNNING"))
        proc = subprocess.Popen(
            [sys.executable, "-m", "supervisor", "run", str(tmp_repo)],
            cwd=str(PROJECT_DIR),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        try:
            layout = Layout(tmp_repo)
            assert _wait_for(
                lambda: (
                    (rt := RuntimeStore(layout).load()) is not None
                    and rt.status == SupervisorStatus.RUNNING_PARENT
                )
            ), "first supervisor did not reach RUNNING_PARENT"

            from supervisor.cli import main

            rc = main(["run", str(tmp_repo)])
            assert rc == 2  # 锁被持有 → 拒绝
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()


class TestBudgetIncludesPreWaitTime:
    def test_budget_counts_active_time_before_wait_human(self, tmp_repo):
        """进入 WAIT_HUMAN 前的活跃时长必须计入墙钟预算（修复前会丢失）。"""
        from supervisor.config import default_config as dc
        from supervisor.engine import SupervisorEngine
        from supervisor.storage import Layout as L

        from conftest import FakeParentRunner, Step, event_names, run_engine, wait_until

        cfg = dc()
        cfg.restart.backoff_seconds = [0.01]
        cfg.limits.max_active_wall_seconds = 1000  # 只测计费，不测限额
        runner = FakeParentRunner([Step(status="WAIT_HUMAN", delay=0.4)], L(tmp_repo))
        engine = SupervisorEngine(base_dir=tmp_repo, config=cfg, runner=runner)

        async def control(eng):
            await wait_until(lambda: "WAIT_HUMAN" in event_names(eng))
            rt = RuntimeStore(Layout(tmp_repo)).load()
            accrued = float(rt.active_budget["accrued_seconds"])
            # 0.4s 的激活时长被计入；修复前此处约为 0
            assert accrued >= 0.3, f"active time before WAIT_HUMAN was not accrued: {accrued}"
            eng.request_stop()

        rc = run_engine(engine, control)
        assert rc == 0
        rt = RuntimeStore(Layout(tmp_repo)).load()
        assert rt.status == SupervisorStatus.STOPPED_OPERATOR