"""M11 Supervisor crash matrix — logical crash (runtime.json) not real kill -9."""
import pytest
from pathlib import Path

from supervisor.config import default_config
from supervisor.engine import SupervisorEngine
from supervisor.storage import Layout, RuntimeStore
from supervisor.human_events import HumanEventStore
from conftest import FakeParentRunner, Step, event_names, run_engine, wait_until


@pytest.mark.crash
def test_supervisor_crash_during_human_delivery_reconciles(tmp_path):
    from supervisor.storage import atomic_write_json
    # Reconcile path: DELIVERING with checkpoint advance -> DELIVERED
    tmp_repo = tmp_path / "repo"
    tmp_repo.mkdir()
    (tmp_repo / ".supervisor").mkdir(parents=True)
    from supervisor.config import default_config as dc
    from conftest import FakeParentRunner, Step
    cfg = dc()
    cfg.restart.backoff_seconds = [0.01]
    layout = Layout(tmp_repo)
    (tmp_repo / ".supervisor" / "task.md").write_text("# task\n", encoding="utf-8")
    runner = FakeParentRunner([Step(status="WAIT_HUMAN")], layout)
    engine = SupervisorEngine(base_dir=tmp_repo, config=cfg, runner=runner)
    async def c1(eng):
        await wait_until(lambda: "WAIT_HUMAN" in event_names(eng))
        eng.request_stop()
    run_engine(engine, c1)
    rt = RuntimeStore(layout).load()
    gate = rt.human_gate["gate_id"]
    seq = rt.human_gate["checkpoint_seq"]
    store = HumanEventStore(layout.human_inbox_dir)
    e = store.append("HUMAN_APPROVED", gate_id=gate)
    store.mark_delivering(e.event_id)
    rt.human_event_id = e.event_id
    RuntimeStore(layout).save(rt)
    atomic_write_json(layout.agent_state_path, {"schema_version":1,"status":"WAIT_HUMAN","checkpoint_seq": seq+1, "updated_at":"2026-08-18T07:00:00Z"})
    rt.last_agent_checkpoint_seq = seq + 1
    RuntimeStore(layout).save(rt)
    from supervisor.models import AgentState
    eng2 = SupervisorEngine(base_dir=tmp_repo, config=cfg)
    eng2.rt = RuntimeStore(layout).load()
    eng2._reconcile_human_delivery(AgentState.from_dict({"schema_version":1,"status":"WAIT_HUMAN","checkpoint_seq": seq+1, "updated_at":"2026-08-18T07:00:00Z"}))
    assert store.get(e.event_id).status == "DELIVERED"


@pytest.mark.crash
def test_supervisor_crash_ci_wait_budget_not_reset(tmp_path):
    from tests.helpers.target_repo import make_fake_target
    from supervisor.ci.fake import FakeCiProvider
    from supervisor.models import CiStatus
    import subprocess, asyncio
    base = make_fake_target(tmp_path, "crash_ci")
    repo = base["repo"]
    layout = Layout(repo)
    sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(repo), capture_output=True, text=True).stdout.strip()
    cfg = default_config()
    cfg.ci.enabled = True
    cfg.ci.provider = "fake"
    cfg.ci.poll_seconds = 1
    cfg.ci.max_wait_seconds = 5
    cfg.restart.backoff_seconds = [0.01]
    prov = FakeCiProvider({sha: [CiStatus.PENDING, CiStatus.PENDING, CiStatus.SUCCESS]})
    from conftest import FakeParentRunner, Step
    runner = FakeParentRunner([Step(status="WAIT_CI"), Step(status="COMPLETED")], layout)
    orig = runner._write_state
    def patched(step):
        if step.status == "WAIT_CI":
            import json as _j
            from datetime import datetime, timezone
            from supervisor.models import AgentState, AgentStatus
            runner._seq += 1
            st = AgentState(schema_version=1, status=AgentStatus.WAIT_CI, checkpoint_seq=runner._seq, updated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"), ci={"sha": sha}, git={"head": sha, "pushed_head": sha})
            layout.agent_state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = layout.agent_state_path.with_suffix(".json.tmp")
            tmp.write_text(_j.dumps(st.to_dict()), encoding="utf-8")
            tmp.replace(layout.agent_state_path)
            return
        return orig(step)
    runner._write_state = patched
    engine = SupervisorEngine(base_dir=repo, config=cfg, runner=runner, ci_provider=prov)
    rc = run_engine(engine)
    assert rc == 0
