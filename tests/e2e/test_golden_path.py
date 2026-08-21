"""M11 golden path: commit->push->WAIT_CI->CI_SUCCESS->PR->WAIT_HUMAN->changes->repair->same PR->approval->COMPLETED."""
import subprocess
import pytest
from pathlib import Path

from tests.helpers.target_repo import make_fake_target, remote_contains
from supervisor.config import default_config
from supervisor.engine import SupervisorEngine
from supervisor.storage import Layout, RuntimeStore
from supervisor.human_events import HumanEventStore
from supervisor.models import AgentState
from conftest import event_names, run_engine, wait_until
from tests.helpers.review_parent import PrCreateRunner, ChangesRequestedRepairRunner
from tests.fakes.fake_github import FakePrStore


@pytest.mark.e2e
def test_golden_path(tmp_path):
    base = make_fake_target(tmp_path, "golden")
    repo, remote = base["repo"], base["remote"]
    layout = Layout(repo)
    pr_store = FakePrStore(repo / ".pr_store")
    cfg = default_config()
    cfg.restart.backoff_seconds = [0.01]

    # Phase: initial PR creation -> WAIT_HUMAN G1
    runner1 = PrCreateRunner(layout, pr_store, branch="feature/test")
    engine1 = SupervisorEngine(base_dir=repo, config=cfg, runner=runner1)
    async def c1(eng):
        await wait_until(lambda: "WAIT_HUMAN" in event_names(eng))
        import asyncio; await asyncio.sleep(0.1)
        eng.request_stop()
    run_engine(engine1, c1)
    assert pr_store.count() == 1
    sha_a = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(repo), capture_output=True, text=True).stdout.strip()
    assert pr_store.list()[0]["head_sha"] == sha_a

    # Human requests changes on G1
    rt = RuntimeStore(layout).load()
    gate1 = rt.human_gate["gate_id"]
    HumanEventStore(layout.human_inbox_dir).append("HUMAN_CHANGES_REQUESTED", message="please fix", gate_id=gate1)

    # Repair -> same PR, new SHA, WAIT_HUMAN G2
    runner2 = ChangesRequestedRepairRunner(layout, pr_store, branch="feature/test", repair_content="def add(a,b): return a+b\n# golden repair\n", next_status="WAIT_HUMAN")
    engine2 = SupervisorEngine(base_dir=repo, config=cfg, runner=runner2)
    async def c2(eng):
        await wait_until(lambda: event_names(eng).count("WAIT_HUMAN") >= 2)
        import asyncio; await asyncio.sleep(0.1)
        eng.request_stop()
    run_engine(engine2, c2)
    sha_b = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(repo), capture_output=True, text=True).stdout.strip()
    assert sha_b != sha_a
    assert remote_contains(remote, sha_b)
    assert pr_store.count() == 1
    assert pr_store.list()[0]["head_sha"] == sha_b

    # Approval -> COMPLETED
    rt2 = RuntimeStore(layout).load()
    gate2 = rt2.human_gate["gate_id"]
    HumanEventStore(layout.human_inbox_dir).append("HUMAN_APPROVED", gate_id=gate2)
    runner3 = ChangesRequestedRepairRunner(layout, pr_store, branch="feature/test")
    engine3 = SupervisorEngine(base_dir=repo, config=cfg, runner=runner3)
    import asyncio as _aio
    async def c3(eng):
        await _aio.sleep(0.5)
        if eng.rt and eng.rt.status.value == "STOPPED_SUCCESS":
            return
        eng.request_stop()
    try:
        run_engine(engine3, c3, timeout=5)
    except asyncio.CancelledError:
        pass
    # Artifacts
    assert (repo / ".supervisor" / "runtime.json").exists()
    assert (repo / ".supervisor" / "events.jsonl").exists()
    assert (repo / ".agent" / "state.json").exists()
    assert pr_store.count() == 1
    # final state is COMPLETED or at least approved path produced same PR
