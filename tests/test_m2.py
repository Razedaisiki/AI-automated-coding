"""M2 — DshRunner 测试（红绿循环，零 LLM，用 fake dsh 可执行脚本）。"""

import asyncio
import json
import os
import time
from pathlib import Path

import pytest

from supervisor.dsh_runner import DshRunner, RunnerError
from supervisor.models import AgentStatus

FAKE_DSH = Path(__file__).parent / "fixtures" / "fake_dsh.py"

PROMPT = (
    "SUPERVISOR EVENT: INITIAL_START\n"
    "You are the Parent Agent for this repository. Continue autonomously."
)


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture
def repo(tmp_path):
    (tmp_path / ".supervisor").mkdir()
    return tmp_path


def _run_dir(repo, activation_id=1):
    d = repo / ".supervisor" / "runs" / f"activation-{activation_id:06d}"
    return d


def _state(status="RUNNING", seq=1):
    return json.dumps(
        {
            "schema_version": 1,
            "status": status,
            "checkpoint_seq": seq,
            "updated_at": "2026-08-18T06:00:00Z",
        }
    )


def _runner(grace=2):
    return DshRunner(executable=str(FAKE_DSH), profile="headless", terminate_grace_seconds=grace)


class TestDshRunnerBasics:
    def test_exit_zero_creates_run_dir_and_logs(self, repo, monkeypatch):
        monkeypatch.setenv("FAKE_DSH_MODE", "exit0")
        monkeypatch.setenv("FAKE_DSH_STATE", _state("RUNNING"))
        rd = _run_dir(repo)
        res = _run(_runner().run(
            repo=repo, prompt=PROMPT, activation_id=1,
            timeout_seconds=30, run_dir=rd,
        ))
        assert res.exit_code == 0
        assert res.timed_out is False
        assert res.pid is not None and res.process_start_id is not None
        assert rd.exists()
        assert (rd / "stdout.log").exists()
        assert (rd / "stderr.log").exists()
        stdout = (rd / "stdout.log").read_text(encoding="utf-8")
        assert str(repo) in stdout          # cwd 必须是 repo
        assert PROMPT[:50] in stdout        # prompt 必须传进去
        # Parent 协议：fake dsh 写了 .agent/state.json
        st = json.loads((repo / ".agent" / "state.json").read_text(encoding="utf-8"))
        assert st["status"] == AgentStatus.RUNNING.value
        assert st["checkpoint_seq"] == 1

    def test_exit_one(self, repo, monkeypatch):
        monkeypatch.setenv("FAKE_DSH_MODE", "exit1")
        res = _run(_runner().run(
            repo=repo, prompt=PROMPT, activation_id=1,
            timeout_seconds=30, run_dir=_run_dir(repo),
        ))
        assert res.exit_code == 1
        assert res.timed_out is False

    def test_run_dir_created_even_when_missing(self, repo, monkeypatch):
        monkeypatch.setenv("FAKE_DSH_MODE", "exit0")
        rd = repo / "x" / "activation-000001"  # 目录不存在，runner 应自建
        res = _run(_runner().run(
            repo=repo, prompt=PROMPT, activation_id=1,
            timeout_seconds=30, run_dir=rd,
        ))
        assert res.exit_code == 0
        assert (rd / "stdout.log").exists()


class TestDshRunnerTimeout:
    def test_timeout_sigterm_cooperative(self, repo, monkeypatch):
        """超时后 SIGTERM 进程组；若进程配合（默认 handler）应立即退出。"""
        monkeypatch.setenv("FAKE_DSH_MODE", "hang")
        t0 = time.monotonic()
        res = _run(_runner(grace=5).run(
            repo=repo, prompt=PROMPT, activation_id=1,
            timeout_seconds=1, run_dir=_run_dir(repo),
        ))
        elapsed = time.monotonic() - t0
        assert res.timed_out is True
        assert res.exit_code is not None and res.exit_code < 0  # 被信号终止
        assert elapsed < 4.0  # 在 grace 内退出，而不是等满 60s
        with pytest.raises(ProcessLookupError):
            os.kill(res.pid, 0)  # 进程组已清

    def test_timeout_escalates_to_sigkill(self, repo, monkeypatch):
        """进程忽略 SIGTERM → 等 grace → SIGKILL 进程组。"""
        monkeypatch.setenv("FAKE_DSH_MODE", "ignore_term_and_hang")
        t0 = time.monotonic()
        res = _run(_runner(grace=1).run(
            repo=repo, prompt=PROMPT, activation_id=1,
            timeout_seconds=1, run_dir=_run_dir(repo),
        ))
        elapsed = time.monotonic() - t0
        assert res.timed_out is True
        assert res.exit_code is not None and res.exit_code < 0
        assert elapsed < 8.0  # 1s timeout + 1s grace + 余量；绝不等 60s
        with pytest.raises(ProcessLookupError):
            os.kill(res.pid, 0)

    def test_no_timeout_within_budget(self, repo, monkeypatch):
        monkeypatch.setenv("FAKE_DSH_MODE", "exit1")
        t0 = time.monotonic()
        res = _run(_runner(grace=1).run(
            repo=repo, prompt=PROMPT, activation_id=1,
            timeout_seconds=30, run_dir=_run_dir(repo),
        ))
        assert time.monotonic() - t0 < 10
        assert res.timed_out is False
        assert res.exit_code == 1


class TestDshRunnerErrors:
    def test_missing_executable_raises_clear_error(self, repo):
        runner = DshRunner(executable="/nonexistent/dsh", profile="headless")
        with pytest.raises(RunnerError) as ei:
            _run(runner.run(
                repo=repo, prompt=PROMPT, activation_id=1,
                timeout_seconds=5, run_dir=_run_dir(repo),
            ))
        assert "dsh" in str(ei.value) or "executable" in str(ei.value).lower()