"""M9 — Parent Delivery Contract (AgentState validation + E2E delivery)."""
import subprocess
from pathlib import Path

from supervisor.models import AgentState, AgentStateError


class TestWaitCIValidation:
    def test_valid_wait_ci_with_matching_git(self):
        AgentState.from_dict({"schema_version":1,"status":"WAIT_CI","checkpoint_seq":1,"updated_at":"2026-08-18T06:00:00Z","ci":{"sha":"a"*40},"git":{"head":"a"*40,"pushed_head":"a"*40}})

    def test_wait_ci_bare_raises(self):
        try:
            AgentState.from_dict({"schema_version":1,"status":"WAIT_CI","checkpoint_seq":1,"updated_at":"2026-08-18T06:00:00Z"})
            assert False, "bare WAIT_CI without ci.sha should be rejected (exact 40-hex invariant)"
        except AgentStateError:
            pass

    def test_invalid_sha_raises(self):
        try:
            AgentState.from_dict({"schema_version":1,"status":"WAIT_CI","checkpoint_seq":1,"updated_at":"2026-08-18T06:00:00Z","ci":{"sha":"not-hex!"}})
            assert False
        except AgentStateError:
            pass

    def test_git_mismatch_raises(self):
        try:
            AgentState.from_dict({"schema_version":1,"status":"WAIT_CI","checkpoint_seq":1,"updated_at":"2026-08-18T06:00:00Z","ci":{"sha":"a"*40},"git":{"head":"b"*40,"pushed_head":"a"*40}})
            assert False
        except AgentStateError:
            pass

class TestReviewValidation:
    def test_valid_review(self):
        AgentState.from_dict({"schema_version":1,"status":"RUNNING","checkpoint_seq":1,"updated_at":"2026-08-18T06:00:00Z","review":{"provider":"github","pr_number":123,"pr_url":"https://example.com/pr/123","head_sha":"a"*40}})

    def test_invalid_pr_number(self):
        try:
            AgentState.from_dict({"schema_version":1,"status":"RUNNING","checkpoint_seq":1,"updated_at":"2026-08-18T06:00:00Z","review":{"pr_number":"x"}})
            assert False
        except AgentStateError:
            pass

    def test_parent_policy_contains_delivery_loop(self):
        from supervisor.prompts import get_parent_policy
        pol = get_parent_policy()
        assert "Standard Delivery Loop" in pol
        assert "WAIT_CI" in pol
        assert "Lookup existing PR" in pol


class TestDeliveryE2E:
    def test_delivery_to_wait_ci_e2e(self, tmp_path):
        from tests.helpers.target_repo import make_fake_target, remote_contains
        from supervisor.config import default_config
        from supervisor.engine import SupervisorEngine
        from supervisor.storage import Layout, RuntimeStore
        from supervisor.models import SupervisorStatus
        from conftest import event_names, run_engine
        from tests.helpers.scripted_parent import SimpleDeliveryRunner

        base = make_fake_target(tmp_path, "m9a")
        repo, remote = base["repo"], base["remote"]
        layout = Layout(repo)
        cfg = default_config()
        cfg.ci.enabled = False
        cfg.restart.backoff_seconds = [0.01]
        runner = SimpleDeliveryRunner(layout, new_content="def add(a,b): return a+b\n# m9 delivery\n")
        engine = SupervisorEngine(base_dir=repo, config=cfg, runner=runner)
        import asyncio
        async def control(eng):
            import conftest as cf
            await cf.wait_until(lambda: "WAIT_CI" in cf.event_names(eng) or "WAITING_CI" in cf.event_names(eng))
            await asyncio.sleep(0.2)
            eng.request_stop()
        rc = run_engine(engine, control)
        assert rc == 0 or engine.rt is not None
        # real commit + push assertions
        sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(repo), capture_output=True, text=True).stdout.strip()
        assert len(sha) == 40
        assert remote_contains(remote, sha)
        st = AgentState.from_dict(__import__("json").loads((repo / ".agent" / "state.json").read_text(encoding="utf-8")))
        assert st.status.value == "WAIT_CI"
        assert st.ci["sha"] == sha
        assert st.git["head"] == sha and st.git["pushed_head"] == sha

    def test_delivery_repairs_before_commit(self, tmp_path):
        from tests.helpers.target_repo import make_fake_target, remote_contains
        from supervisor.config import default_config
        from supervisor.engine import SupervisorEngine
        from supervisor.storage import Layout
        from tests.helpers.scripted_parent import RepairBeforeCommitRunner
        from conftest import event_names, run_engine
        import asyncio
        base = make_fake_target(tmp_path, "m9b")
        repo, remote = base["repo"], base["remote"]
        before_sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(repo), capture_output=True, text=True).stdout.strip()
        layout = Layout(repo)
        cfg = default_config()
        cfg.ci.enabled = False
        cfg.restart.backoff_seconds = [0.01]
        buggy = "def add(a,b): return a-b\n"
        fixed = "def add(a,b): return a+b\n# fixed\n"
        runner = RepairBeforeCommitRunner(layout, buggy=buggy, fixed=fixed)
        engine = SupervisorEngine(base_dir=repo, config=cfg, runner=runner)
        async def control(eng):
            import conftest as cf
            await cf.wait_until(lambda: "WAIT_CI" in cf.event_names(eng) or "WAITING_CI" in cf.event_names(eng))
            await asyncio.sleep(0.2)
            eng.request_stop()
        rc = run_engine(engine, control)
        sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(repo), capture_output=True, text=True).stdout.strip()
        assert sha != before_sha
        assert remote_contains(remote, sha)
        assert runner.push_shas and runner.push_shas[0] == sha
        # only validated commit was pushed
        assert subprocess.run(["python3", "-m", "pytest", "-q"], cwd=str(repo), capture_output=True).returncode == 0
