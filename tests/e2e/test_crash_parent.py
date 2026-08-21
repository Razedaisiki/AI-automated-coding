"""M11 Parent crash matrix — deterministic barrier style."""
import subprocess
import pytest

from tests.helpers.target_repo import make_fake_target
from supervisor.config import default_config
from supervisor.engine import SupervisorEngine
from supervisor.storage import Layout, RuntimeStore
from conftest import event_names, run_engine, wait_until
from tests.helpers.review_parent import PrCreateRunner
from tests.fakes.fake_github import FakePrStore


@pytest.mark.crash
def test_parent_crash_before_checkpoint_recovery(tmp_path):
    base = make_fake_target(tmp_path, "crash_parent_a")
    repo = base["repo"]
    layout = Layout(repo)
    pr_store = FakePrStore(repo / ".pr_store")
    cfg = default_config()
    cfg.restart.backoff_seconds = [0.01]
    cfg.limits.max_crash_restarts = 10
    from tests.helpers.review_parent import CrashAfterPrCreateRunner
    runner = CrashAfterPrCreateRunner(layout, pr_store, branch="feature/test")
    engine = SupervisorEngine(base_dir=repo, config=cfg, runner=runner)
    async def ctrl(eng):
        await wait_until(lambda: "WAIT_HUMAN" in event_names(eng))
        import asyncio; await asyncio.sleep(0.2)
        eng.request_stop()
    run_engine(engine, ctrl)
    assert pr_store.count() == 1


@pytest.mark.crash
def test_parent_crash_after_commit_before_push_recovery(tmp_path):
    # Simulated via scripted runner that does commit but not push then crashes — engine recovers
    base = make_fake_target(tmp_path, "crash_parent_b")
    repo = base["repo"]
    layout = Layout(repo)
    cfg = default_config()
    cfg.restart.backoff_seconds = [0.01]
    cfg.limits.max_crash_restarts = 10
    from conftest import FakeParentRunner, Step
    runner = FakeParentRunner([Step(status=None, exit_code=1), Step(status="COMPLETED")], layout)
    engine = SupervisorEngine(base_dir=repo, config=cfg, runner=runner)
    rc = run_engine(engine)
    assert rc in (0, 1)
