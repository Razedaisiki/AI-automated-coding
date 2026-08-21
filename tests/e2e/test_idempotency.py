"""M11 Idempotency matrix — PR/CI/Human reuse not blind create."""
import pytest

from tests.helpers.target_repo import make_fake_target
from supervisor.config import default_config
from supervisor.engine import SupervisorEngine
from supervisor.storage import Layout, RuntimeStore
from supervisor.human_events import HumanEventStore
from conftest import event_names, run_engine, wait_until
from tests.helpers.review_parent import PrCreateRunner
from tests.fakes.fake_github import FakePrStore


@pytest.mark.crash
def test_pr_idempotency_same_pr_after_repair(tmp_path):
    import subprocess
    base = make_fake_target(tmp_path, "idem_a")
    repo = base["repo"]
    layout = Layout(repo)
    pr_store = FakePrStore(repo / ".pr_store")
    cfg = default_config()
    cfg.restart.backoff_seconds = [0.01]
    runner1 = PrCreateRunner(layout, pr_store, branch="feature/test")
    engine1 = SupervisorEngine(base_dir=repo, config=cfg, runner=runner1)
    async def c1(eng):
        await wait_until(lambda: "WAIT_HUMAN" in event_names(eng))
        import asyncio; await asyncio.sleep(0.1)
        eng.request_stop()
    run_engine(engine1, c1)
    assert pr_store.count() == 1
    from tests.helpers.review_parent import ChangesRequestedRepairRunner
    rt = RuntimeStore(layout).load()
    gate = rt.human_gate["gate_id"]
    HumanEventStore(layout.human_inbox_dir).append("HUMAN_CHANGES_REQUESTED", message="fix", gate_id=gate)
    runner2 = ChangesRequestedRepairRunner(layout, pr_store, branch="feature/test", repair_content="def add(a,b): return a+b\n# idem\n", next_status="WAIT_HUMAN")
    engine2 = SupervisorEngine(base_dir=repo, config=cfg, runner=runner2)
    async def c2(eng):
        await wait_until(lambda: event_names(eng).count("WAIT_HUMAN") >= 2)
        import asyncio; await asyncio.sleep(0.1)
        eng.request_stop()
    run_engine(engine2, c2)
    assert pr_store.count() == 1


@pytest.mark.crash
def test_human_event_no_stale_across_gate(tmp_path):
    tmp_repo = tmp_path / "repo"
    tmp_repo.mkdir()
    (tmp_repo / ".supervisor").mkdir(parents=True)
    from supervisor.config import default_config as dc
    from supervisor.human_events import HumanEventStore
    store = HumanEventStore(tmp_repo / ".supervisor" / "inbox" / "human")
    e1 = store.append("HUMAN_APPROVED", gate_id="gate-a")
    assert store.next_pending(gate_id="gate-b") is None
    assert store.next_pending(gate_id="gate-a").event_id == e1.event_id
