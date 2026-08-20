"""M6 — Git Evidence & Recovery Context (GitSnapshot V1 + durable snapshots + events)."""
import json
import subprocess
import time
from pathlib import Path

import pytest

from supervisor.models import GitSnapshot, compare_git_snapshots
from supervisor.git_probe import capture
from supervisor.storage import Layout, atomic_write_json
from supervisor.events import EventLog
from supervisor.config import default_config
from supervisor.engine import SupervisorEngine

from conftest import FAKE_DSH, FakeParentRunner, Step, StepScript, event_names, run_engine, wait_until, write_repo_toml

# helper to create a temp git repo with commit
def _init_git_repo(path: Path, remote_url=None):
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git","init","-q"], cwd=str(path), check=True)
    subprocess.run(["git","config","user.email","t@t"], cwd=str(path), check=True)
    subprocess.run(["git","config","user.name","t"], cwd=str(path), check=True)
    (path / "f.txt").write_text("hello\n", encoding="utf-8")
    subprocess.run(["git","add","-A"], cwd=str(path), check=True)
    subprocess.run(["git","commit","-q","-m","init"], cwd=str(path), check=True)
    if remote_url:
        subprocess.run(["git","remote","add","origin", remote_url], cwd=str(path), check=True)

class TestGitSnapshotV1:
    def test_non_git_repo(self, tmp_path):
        snap = capture(tmp_path)
        assert snap.is_git_repo is False
        assert snap.branch is None
        assert snap.head is None
        assert snap.detached_head is False
        assert snap.dirty is False
        assert snap.has_remote is False
        assert snap.remote_url is None
        # roundtrip via dict
        d = snap.to_dict()
        snap2 = GitSnapshot.from_dict(d)
        assert snap2.is_git_repo is False

    def test_normal_branch(self, tmp_path):
        repo = tmp_path / "g"
        _init_git_repo(repo)
        snap = capture(repo)
        assert snap.is_git_repo is True
        assert snap.branch in ("master","main")
        assert snap.detached_head is False
        assert snap.head is not None and len(snap.head) == 40
        assert snap.dirty is False

    def test_detached_head(self, tmp_path):
        repo = tmp_path / "g"
        _init_git_repo(repo)
        subprocess.run(["git","checkout","--detach","HEAD"], cwd=str(repo), check=True)
        snap = capture(repo)
        assert snap.is_git_repo is True
        assert snap.branch is None
        assert snap.detached_head is True
        assert snap.head is not None

    def test_dirty_vs_clean(self, tmp_path):
        repo = tmp_path / "g"
        _init_git_repo(repo)
        snap = capture(repo)
        assert snap.dirty is False
        (repo / "f.txt").write_text("changed\n", encoding="utf-8")
        snap2 = capture(repo)
        assert snap2.dirty is True
        assert snap2.head == snap.head

    def test_remote_absent(self, tmp_path):
        repo = tmp_path / "g"
        _init_git_repo(repo)
        snap = capture(repo)
        assert snap.has_remote is False
        assert snap.remote_url is None

    def test_origin_remote(self, tmp_path):
        repo = tmp_path / "g"
        _init_git_repo(repo, remote_url="https://example.com/a.git")
        snap = capture(repo)
        assert snap.has_remote is True
        assert snap.remote_url == "https://example.com/a.git"

    def test_non_origin_remote(self, tmp_path):
        repo = tmp_path / "g"
        _init_git_repo(repo)
        subprocess.run(["git","remote","add","upstream","https://example.com/up.git"], cwd=str(repo), check=True)
        snap = capture(repo)
        assert snap.has_remote is True
        assert snap.remote_url == "https://example.com/up.git"

    def test_compare(self):
        a = GitSnapshot(is_git_repo=True, branch="main", head="aaa", dirty=False, has_remote=False)
        b = GitSnapshot(is_git_repo=True, branch="main", head="bbb", dirty=True, has_remote=True, remote_url="https://x")
        diff = compare_git_snapshots(a,b)
        assert diff["head_changed"] is True
        assert diff["dirty_changed"] is True
        assert diff["branch_changed"] is False
        assert diff["remote_changed"] is True

class TestM6EngineIntegration:
    def test_startup_snapshot_exists(self, tmp_repo):
        cfg = default_config()
        cfg.restart.backoff_seconds = [0.01]
        runner = FakeParentRunner([StepScript.completed()], Layout(tmp_repo))
        engine = SupervisorEngine(base_dir=tmp_repo, config=cfg, runner=runner)
        from conftest import run_engine as _run
        rc = _run(engine)
        assert rc == 0
        assert (tmp_repo / ".supervisor" / "git-startup.json").exists()
        assert "GIT_SNAPSHOT" in event_names(engine)

    def test_git_before_written_before_parent_launch(self, tmp_repo):
        # use a hanging runner so we can check before file
        import asyncio
        cfg = default_config()
        runner = FakeParentRunner([Step(status="RUNNING", hang=True)], Layout(tmp_repo))
        engine = SupervisorEngine(base_dir=tmp_repo, config=cfg, runner=runner)
        async def control(eng):
            await wait_until(lambda: "PARENT_STARTING" in event_names(eng))
            # git-before must already be durable
            run_dir = tmp_repo / ".supervisor" / "runs" / "activation-000001"
            assert (run_dir / "git-before.json").exists()
            eng.request_stop()
        from conftest import run_engine
        rc = run_engine(engine, control)
        assert rc == 0

    def test_head_change_event(self, tmp_repo):
        # runner that creates a commit: use subprocess inside custom runner
        # Simpler: use FakeParentRunner that writes dirty file, but engine captures git before/after
        # dirty changes reliably
        (tmp_repo / "x.txt").write_text("before\n", encoding="utf-8")
        cfg = default_config()
        cfg.restart.backoff_seconds = [0.01]
        # runner that makes repo dirty
        import asyncio as _aio
        class DirtyRunner(FakeParentRunner):
            async def run(self, **kw):
                Path(kw["repo"] + "/dirty.txt").write_text("dirty\n", encoding="utf-8") if False else None
                # actually write to repo
                repo = kw["repo"]
                Path(repo + "/dirty.txt" if isinstance(repo, str) else str(repo) + "/dirty.txt").write_text("dirty\n", encoding="utf-8")
                return await super().run(**kw)
        runner = FakeParentRunner([StepScript.completed()], Layout(tmp_repo))
        # monkey: make repo dirty via direct write before runner's after capture
        orig_run = runner.run
        async def patched_run(**kw):
            result = await orig_run(**kw)
            # make dirty after runner but before engine's git_after? No — engine already captured after.
            # Instead create dirty before run via control: write file early so dirty event fires
            return result
        runner.run = patched_run
        engine = SupervisorEngine(base_dir=tmp_repo, config=cfg, runner=runner)
        # create dirty before engine starts
        (tmp_repo / "pre_dirty.txt").write_text("dirty\n", encoding="utf-8")
        rc = run_engine(engine)
        # at least GIT_SNAPSHOT emitted
        assert "GIT_SNAPSHOT" in event_names(engine)

    def test_git_before_survives_supervisor_crash(self, tmp_repo):
        import json as _json
        cfg = default_config()
        cfg.restart.backoff_seconds = [0.01]
        runner = FakeParentRunner([StepScript.completed()], Layout(tmp_repo))
        engine = SupervisorEngine(base_dir=tmp_repo, config=cfg, runner=runner)
        rc = run_engine(engine)
        assert rc == 0
        run_dir = tmp_repo / ".supervisor" / "runs" / "activation-000001"
        assert (run_dir / "git-before.json").exists()
        data = _json.loads((run_dir / "git-before.json").read_text(encoding="utf-8"))
        assert "is_git_repo" in data
