"""CLI 测试：init / run / parent-once / status / events / resume / stop。"""

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from supervisor.cli import main
from supervisor.config import Config, default_config
from supervisor.events import EventLog
from supervisor.models import RuntimeState, StopReason, SupervisorStatus
from supervisor.storage import Layout, RuntimeStore

from conftest import FAKE_DSH, PROJECT_DIR, write_repo_toml


def completed_state_json():
    return json.dumps(
        {
            "schema_version": 1,
            "status": "COMPLETED",
            "checkpoint_seq": 1,
            "updated_at": "2026-08-18T06:00:00Z",
        }
    )


def running_state_json():
    return json.dumps(
        {
            "schema_version": 1,
            "status": "RUNNING",
            "checkpoint_seq": 1,
            "updated_at": "2026-08-18T06:00:00Z",
        }
    )


class TestInit:
    def test_init_creates_layout_and_toml(self, tmp_repo, capsys):
        rc = main(["init", str(tmp_repo)])
        assert rc == 0
        assert (tmp_repo / ".supervisor" / "runs").is_dir()
        assert (tmp_repo / ".supervisor" / "inbox").is_dir()
        cfg_path = tmp_repo / "supervisor.toml"
        assert cfg_path.exists()
        assert "max_parent_activations" in cfg_path.read_text(encoding="utf-8")
        assert "Supervisor already" not in capsys.readouterr().out


class TestRunCli:
    def test_run_fresh_task_to_completed(self, tmp_repo, monkeypatch):
        monkeypatch.setenv("FAKE_DSH_MODE", "exit0")
        monkeypatch.setenv("FAKE_DSH_STATE", completed_state_json())
        write_repo_toml(tmp_repo, default_config(), fake_dsh=True)
        rc = main(["run", str(tmp_repo)])
        assert rc == 0
        rt = RuntimeStore(Layout(tmp_repo)).load()
        assert rt.status == SupervisorStatus.STOPPED_SUCCESS
        assert rt.stop_reason == StopReason.TASK_COMPLETED

    def test_run_clean_loop_enforces_activations(self, tmp_repo, monkeypatch):
        cfg = default_config()
        cfg.limits.max_parent_activations = 3
        cfg.restart.backoff_seconds = [0.01]
        monkeypatch.setenv("FAKE_DSH_MODE", "exit0")
        monkeypatch.setenv("FAKE_DSH_STATE", running_state_json())
        write_repo_toml(tmp_repo, cfg, fake_dsh=True)
        rc = main(["run", str(tmp_repo)])
        assert rc == 1
        rt = RuntimeStore(Layout(tmp_repo)).load()
        assert rt.stop_reason == StopReason.MAX_PARENT_ACTIVATIONS
        starts = EventLog(Layout(tmp_repo).events_path).events_named("PARENT_STARTED")
        assert len(starts) == 3

    def test_run_missing_config_errors_cleanly(self, tmp_repo, capsys):
        rc = main(["run", str(tmp_repo)])
        assert rc == 1
        captured = capsys.readouterr()
        assert "config file not found" in (captured.out + captured.err)


class TestParentOnce:
    def test_parent_once_exit_zero(self, tmp_repo, monkeypatch, capsys):
        monkeypatch.setenv("FAKE_DSH_MODE", "exit0")
        monkeypatch.setenv("FAKE_DSH_STATE", running_state_json())
        write_repo_toml(tmp_repo, default_config(), fake_dsh=True)
        rc = main(["parent-once", str(tmp_repo), "--prompt", "SUPERVISOR EVENT: INITIAL_START"])
        assert rc == 0
        rd = tmp_repo / ".supervisor" / "runs" / "activation-000001"
        assert (rd / "stdout.log").exists()
        result = json.loads((rd / "result.json").read_text(encoding="utf-8"))
        assert result["exit_code"] == 0
        assert result["timed_out"] is False
        assert "stdout.log" in capsys.readouterr().out  # 结果打印

    def test_parent_once_exit_one(self, tmp_repo, monkeypatch):
        monkeypatch.setenv("FAKE_DSH_MODE", "exit1")
        write_repo_toml(tmp_repo, default_config(), fake_dsh=True)
        rc = main(["parent-once", str(tmp_repo)])
        assert rc == 1
        result = json.loads(
            (tmp_repo / ".supervisor" / "runs" / "activation-000001" / "result.json").read_text(
                encoding="utf-8"
            )
        )
        assert result["exit_code"] == 1


class TestStatusEventsResume:
    def test_status_before_any_run(self, tmp_repo, capsys):
        rc = main(["status", str(tmp_repo)])
        assert rc == 0
        assert "not started" in capsys.readouterr().out

    def test_events_tail(self, tmp_repo, monkeypatch, capsys):
        monkeypatch.setenv("FAKE_DSH_MODE", "exit0")
        monkeypatch.setenv("FAKE_DSH_STATE", completed_state_json())
        write_repo_toml(tmp_repo, default_config(), fake_dsh=True)
        main(["run", str(tmp_repo)])
        rc = main(["events", str(tmp_repo), "--tail", "10"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "SUPERVISOR_STARTED" in out
        assert "PARENT_STARTED" in out

    def test_resume_writes_marker(self, tmp_repo):
        rc = main(["resume", str(tmp_repo), "--event", "HUMAN_APPROVED"])
        assert rc == 0
        marker = json.loads((tmp_repo / ".supervisor" / "resume.json").read_text(encoding="utf-8"))
        assert marker["event"] == "HUMAN_APPROVED"

    def test_resume_rejects_unknown_event(self, tmp_repo):
        rc = main(["resume", str(tmp_repo), "--event", "NOPE"])
        assert rc == 1


class TestCrashRecoveryCli:
    def test_kill9_supervisor_then_restart_adopts_orphan(self, tmp_repo):
        """端到端 M5 验收：kill -9 Supervisor → 重启 → 收养还活着的 Parent，
        绝不启动第二个 Agent；孤儿退出后从 state 继续直至完成。"""
        write_repo_toml(tmp_repo, default_config(), fake_dsh=True)
        layout = Layout(tmp_repo)

        hang_env = dict(
            os.environ,
            FAKE_DSH_MODE="hang",
            FAKE_DSH_STATE=running_state_json(),
        )
        proc1 = subprocess.Popen(
            [sys.executable, "-m", "supervisor", "run", str(tmp_repo)],
            cwd=str(PROJECT_DIR),
            env=hang_env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        try:
            # 1. 等第一个 Parent 启动并记录 pid
            deadline = time.monotonic() + 15
            parent_pid = None
            while time.monotonic() < deadline:
                rt = RuntimeStore(layout).load()
                if rt is not None and rt.current_parent and rt.current_parent.pid:
                    parent_pid = rt.current_parent.pid
                    break
                time.sleep(0.05)
            assert parent_pid is not None, "supervisor never started a parent"
            assert os.path.exists(f"/proc/{parent_pid}")

            # 2. kill -9 Supervisor（模拟崩溃），Parent 继续存活
            proc1.kill()
            proc1.wait()
            rt = RuntimeStore(layout).load()
            assert rt is not None and rt.current_parent and rt.current_parent.pid == parent_pid

            # 3. 重启 Supervisor —— 必须收养孤儿
            exit_env = dict(
                os.environ,
                FAKE_DSH_MODE="exit0",
                FAKE_DSH_STATE=completed_state_json(),
            )
            proc2 = subprocess.Popen(
                [sys.executable, "-m", "supervisor", "run", str(tmp_repo)],
                cwd=str(PROJECT_DIR),
                env=exit_env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
            )
            try:
                deadline = time.monotonic() + 15
                adopted = False
                while time.monotonic() < deadline:
                    evs = EventLog(layout.events_path).read_all()
                    adopted = any(e["event"] == "ORPHAN_ADOPTED" for e in evs)
                    if adopted:
                        break
                    time.sleep(0.05)
                assert adopted, "restarted supervisor did not adopt the orphan"

                # 收养期间绝不允许启动第二个 Parent
                starts = EventLog(layout.events_path).events_named("PARENT_STARTED")
                assert len(starts) == 1

                # 4. 杀掉孤儿 → Supervisor 察觉并继续
                os.killpg(parent_pid, signal.SIGKILL)
                rc = proc2.wait(timeout=20)
                assert rc == 0
                rt = RuntimeStore(layout).load()
                assert rt.status == SupervisorStatus.STOPPED_SUCCESS
                assert rt.stop_reason == StopReason.TASK_COMPLETED
                assert (
                    len(EventLog(layout.events_path).events_named("PARENT_STARTED")) == 2
                )
                starts[0]["activation"]  # 第一次是 activation 1
            finally:
                if proc2.poll() is None:
                    proc2.kill()
                    proc2.wait()
        finally:
            if proc1.poll() is None:
                proc1.kill()
                proc1.wait()
            # 清理孤儿（若仍在）
            try:
                if os.path.exists(f"/proc/{parent_pid}"):
                    os.killpg(parent_pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError, NameError):
                pass


class TestStopCli:
    def test_stop_signals_running_supervisor(self, tmp_repo):
        """端到端：SIGTERM 停止 Supervisor，Parent 进程组必须被清。"""
        write_repo_toml(tmp_repo, default_config(), fake_dsh=True)
        env = dict(
            os.environ,
            FAKE_DSH_MODE="hang",
            FAKE_DSH_STATE=running_state_json(),
        )
        proc = subprocess.Popen(
            [sys.executable, "-m", "supervisor", "run", str(tmp_repo)],
            cwd=str(PROJECT_DIR),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        try:
            layout = Layout(tmp_repo)
            # 等 Parent 启动且 runtime 记录 pid
            deadline = time.monotonic() + 15
            pid = None
            while time.monotonic() < deadline:
                rt = RuntimeStore(layout).load()
                if rt is not None and rt.current_parent and rt.current_parent.pid:
                    pid = rt.current_parent.pid
                    break
                time.sleep(0.1)
            assert pid is not None, "supervisor never started a parent"
            assert os.path.exists(f"/proc/{pid}")
            # 操作员停止
            rc = main(["stop", str(tmp_repo)])
            assert rc == 0
            proc.wait(timeout=15)
            # 收尾状态
            rt = RuntimeStore(layout).load()
            assert rt.status == SupervisorStatus.STOPPED_OPERATOR
            assert rt.stop_reason == StopReason.OPERATOR_STOP
            # Parent 进程组必须被清干净
            time.sleep(0.2)
            try:
                os.kill(pid, 0)
                alive = True
            except ProcessLookupError:
                alive = False
            assert alive is False, "parent process survived supervisor stop"
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()