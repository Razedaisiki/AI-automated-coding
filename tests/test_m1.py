"""M1 — Storage + Lock + Config + Models 测试（红绿循环）。"""

import json

import pytest

from supervisor.config import ConfigError, default_config, load_config
from supervisor.events import EventLog
from supervisor.lock import LockHeldError, SupervisorLock
from supervisor.models import (
    AgentState,
    AgentStateError,
    AgentStatus,
    RuntimeState,
    StopReason,
    SupervisorStatus,
    agent_state_from_dict,
)
from supervisor.storage import (
    Layout,
    RuntimeStore,
    atomic_write_json,
    load_agent_state,
    read_json_strict,
)

PROJECT_TOML = "supervisor.toml"
PROJECT_DIR = __import__("pathlib").Path(__file__).resolve().parent.parent


def _write_toml(base, text=PROJECT_TOML, name="supervisor.toml"):
    (base / name).write_text(text, encoding="utf-8")
    return base / name


# ---------------------------------------------------------------- config


class TestConfig:
    def test_default_config_values(self):
        cfg = default_config()
        assert cfg.dsh.executable == "dsh"
        assert cfg.dsh.profile == "headless"
        assert cfg.limits.max_parent_activations == 20
        assert cfg.limits.parent_timeout_seconds == 2700
        assert cfg.limits.terminate_grace_seconds == 10
        assert cfg.restart.backoff_seconds == [2, 5, 15, 30, 60]
        assert cfg.ci.enabled is False
        assert cfg.human.pause_active_wall_clock is True

    def test_load_project_toml(self):
        toml_path = PROJECT_DIR / PROJECT_TOML
        if not toml_path.exists():
            toml_path = PROJECT_DIR / "supervisor.toml.example"
        cfg = load_config(toml_path)
        assert cfg.version == 1
        assert cfg.dsh.executable == "dsh"
        assert cfg.limits.max_active_wall_seconds == 14400
        assert cfg.restart.backoff_seconds == [2, 5, 15, 30, 60]
        assert cfg.ci.poll_seconds == 30

    def test_missing_file_raises(self, tmp_path):
        with pytest.raises(ConfigError):
            load_config(tmp_path / "nope.toml")

    def test_invalid_toml_raises(self, tmp_path):
        p = _write_toml(tmp_path, "this is [not toml")
        with pytest.raises(ConfigError):
            load_config(p)

    def test_wrong_type_raises(self, tmp_path):
        p = _write_toml(tmp_path, 'version = 1\n[dsh]\nexecutable = 42\n')
        with pytest.raises(ConfigError):
            load_config(p)

    def test_negative_limit_raises(self, tmp_path):
        p = _write_toml(tmp_path, '[limits]\nmax_timeouts = -1\n')
        with pytest.raises(ConfigError):
            load_config(p)


# --------------------------------------------------------------- storage


class TestStorage:
    def test_atomic_write_json_roundtrip(self, tmp_path):
        f = tmp_path / "x.json"
        atomic_write_json(f, {"a": 1, "b": [1, 2]})
        assert read_json_strict(f) == {"a": 1, "b": [1, 2]}

    def test_atomic_write_no_leftover_tmp(self, tmp_path):
        f = tmp_path / "x.json"
        atomic_write_json(f, {"a": 1})
        assert not list(tmp_path.glob("*.tmp"))
        atomic_write_json(f, {"a": 2})
        assert not list(tmp_path.glob("*.tmp"))

    def test_atomic_write_survives_stale_tmp(self, tmp_path):
        """runtime 写到一半进程死 → 原文件不损坏，下次写入覆盖 tmp。"""
        f = tmp_path / "runtime.json"
        atomic_write_json(f, {"status": "OK"})
        (tmp_path / "runtime.json.tmp").write_text('{"status": "HALF', encoding="utf-8")
        atomic_write_json(f, {"status": "NEW"})
        assert read_json_strict(f) == {"status": "NEW"}
        assert not (tmp_path / "runtime.json.tmp").exists()

    def test_read_missing_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            read_json_strict(tmp_path / "missing.json")

    def test_read_bad_json_raises(self, tmp_path):
        f = tmp_path / "bad.json"
        f.write_text("{not json", encoding="utf-8")
        with pytest.raises(ValueError):
            read_json_strict(f)

    def test_runtime_store_roundtrip(self, tmp_path):
        layout = Layout(tmp_path)
        layout.ensure_dirs()
        store = RuntimeStore(layout)
        assert store.load() is None
        state = RuntimeState(
            schema_version=1,
            status=SupervisorStatus.RUNNING_PARENT,
            task_started_at="2026-08-18T05:00:00Z",
            current_parent=None,
            counters=None,
            limits=None,
            last_agent_checkpoint_seq=3,
            supervisor_pid=1000,
            active_budget=None,
            stop_reason=None,
        )
        store.save(state)
        loaded = store.load()
        assert loaded.status == SupervisorStatus.RUNNING_PARENT
        assert loaded.last_agent_checkpoint_seq == 3


# ----------------------------------------------------------- agent state


class TestAgentStateParse:
    def _valid(self):
        return {
            "schema_version": 1,
            "status": "RUNNING",
            "checkpoint_seq": 7,
            "updated_at": "2026-08-18T06:00:00Z",
        }

    def test_parse_valid(self):
        st = agent_state_from_dict(self._valid())
        assert st.status == AgentStatus.RUNNING
        assert st.checkpoint_seq == 7

    def test_missing_checkpoint_seq_raises(self):
        raw = self._valid()
        del raw["checkpoint_seq"]
        with pytest.raises(AgentStateError):
            agent_state_from_dict(raw)

    def test_bad_status_raises(self):
        raw = self._valid()
        raw["status"] = "TESTING"  # Parent 内部语义，Supervisor 不理解
        with pytest.raises(AgentStateError):
            agent_state_from_dict(raw)

    def test_wrong_schema_version_raises(self):
        raw = self._valid()
        raw["schema_version"] = 2
        with pytest.raises(AgentStateError):
            agent_state_from_dict(raw)

    def test_negative_seq_raises(self):
        raw = self._valid()
        raw["checkpoint_seq"] = -1
        with pytest.raises(AgentStateError):
            agent_state_from_dict(raw)

    def test_load_agent_state_missing_returns_none(self, tmp_path):
        assert load_agent_state(tmp_path / "state.json") is None

    def test_load_agent_state_invalid(self, tmp_path):
        f = tmp_path / "state.json"
        f.write_text("{half", encoding="utf-8")
        with pytest.raises(AgentStateError):
            load_agent_state(f)

    def test_roundtrip_to_dict(self):
        st = agent_state_from_dict(self._valid())
        d = st.to_dict()
        assert d["status"] == "RUNNING"
        assert json.loads(json.dumps(d)) == d  # JSON serializable


# ----------------------------------------------------------------- events


class TestEventLog:
    def test_append_and_read(self, tmp_path):
        p = tmp_path / "events.jsonl"
        log = EventLog(p)
        log.emit("SUPERVISOR_STARTED")
        log.emit("PARENT_STARTED", activation=1, pid=123)
        log.emit("PARENT_EXITED", activation=1, exit_code=0)
        rows = log.read_all()
        assert [r["event"] for r in rows] == [
            "SUPERVISOR_STARTED",
            "PARENT_STARTED",
            "PARENT_EXITED",
        ]
        assert rows[1]["activation"] == 1 and rows[1]["pid"] == 123
        assert all("ts" in r for r in rows)

    def test_survives_restart(self, tmp_path):
        p = tmp_path / "events.jsonl"
        EventLog(p).emit("SUPERVISOR_STARTED")
        # 新实例（模拟 Supervisor 重启）续写不覆盖
        EventLog(p).emit("SUPERVISOR_CRASH_RECOVERY")
        rows = EventLog(p).read_all()
        assert len(rows) == 2

    def test_tail(self, tmp_path):
        p = tmp_path / "events.jsonl"
        log = EventLog(p)
        for i in range(10):
            log.emit("EVT", i=i)
        tail = log.tail(3)
        assert [r["i"] for r in tail] == [7, 8, 9]


# ------------------------------------------------------------------- lock


class TestLock:
    def test_exclusive(self, tmp_path):
        lockfile = tmp_path / ".supervisor" / "lock"
        lockfile.parent.mkdir(parents=True)
        l1 = SupervisorLock(lockfile)
        l1.acquire()
        l2 = SupervisorLock(lockfile)
        with pytest.raises(LockHeldError):
            l2.acquire()
        l1.release()
        l2.acquire()  # 释放后可获锁
        l2.release()

    def test_context_manager(self, tmp_path):
        lockfile = tmp_path / "lock"
        with SupervisorLock(lockfile):
            with pytest.raises(LockHeldError):
                SupervisorLock(lockfile).acquire()
        SupervisorLock(lockfile).acquire().release()  # 出上下文后释放