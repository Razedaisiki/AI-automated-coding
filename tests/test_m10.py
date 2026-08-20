"""M10 — PR / Review / Repair Loop."""
import json
from supervisor.models import AgentState

class TestReviewFields:
    def test_review_roundtrip(self):
        raw = {"schema_version":1,"status":"WAIT_HUMAN","checkpoint_seq":2,"updated_at":"2026-08-18T06:00:00Z","review":{"provider":"github","pr_number":7,"pr_url":"https://github.com/o/r/pull/7","head_sha":"a"*40}}
        st = AgentState.from_dict(raw)
        d = st.to_dict()
        assert d["review"]["pr_number"] == 7

    def test_review_optional(self):
        s = AgentState.from_dict({"schema_version":1,"status":"COMPLETED","checkpoint_seq":1,"updated_at":"2026-08-18T06:00:00Z"})
        assert s.review is None

class TestReviewCycleEngine:
    def _gate(self, eng):
        from supervisor.storage import RuntimeStore, Layout
        rt = RuntimeStore(Layout(eng.base)).load()
        return rt.human_gate["gate_id"] if isinstance(getattr(rt, "human_gate", None), dict) else None

    def test_changes_requested_then_approval_cycle(self, tmp_repo):
        from supervisor.config import default_config
        from supervisor.engine import SupervisorEngine
        from supervisor.storage import Layout
        from supervisor.human_events import HumanEventStore
        from conftest import FakeParentRunner, Step, event_names, run_engine, wait_until
        cfg = default_config()
        cfg.restart.backoff_seconds = [0.01]
        runner = FakeParentRunner([
            Step(status="WAIT_HUMAN"),
            Step(status="WAIT_HUMAN"),
            Step(status="COMPLETED"),
        ], Layout(tmp_repo))
        engine = SupervisorEngine(base_dir=tmp_repo, config=cfg, runner=runner)
        async def control(eng):
            await wait_until(lambda: "WAIT_HUMAN" in event_names(eng))
            from supervisor.storage import RuntimeStore
            rt = RuntimeStore(Layout(eng.base)).load()
            gate = rt.human_gate["gate_id"]
            HumanEventStore(eng.layout.human_inbox_dir).append("HUMAN_CHANGES_REQUESTED", message="wrap api", gate_id=gate)
            await wait_until(lambda: event_names(eng).count("WAIT_HUMAN") >= 2)
            rt2 = RuntimeStore(Layout(eng.base)).load()
            gate2 = rt2.human_gate["gate_id"]
            HumanEventStore(eng.layout.human_inbox_dir).append("HUMAN_APPROVED", gate_id=gate2)
        rc = run_engine(engine, control)
        assert rc == 0
        assert runner.calls == [1,2,3]

    def test_repeated_changes_requested(self, tmp_repo):
        from supervisor.config import default_config
        from supervisor.engine import SupervisorEngine
        from supervisor.storage import Layout
        from supervisor.human_events import HumanEventStore
        from conftest import FakeParentRunner, Step, event_names, run_engine, wait_until
        cfg = default_config()
        cfg.restart.backoff_seconds = [0.01]
        runner = FakeParentRunner([
            Step(status="WAIT_HUMAN"),
            Step(status="WAIT_HUMAN"),
            Step(status="WAIT_HUMAN"),
            Step(status="COMPLETED"),
        ], Layout(tmp_repo))
        engine = SupervisorEngine(base_dir=tmp_repo, config=cfg, runner=runner)
        async def control(eng):
            await wait_until(lambda: event_names(eng).count("WAIT_HUMAN") >= 1)
            from supervisor.storage import RuntimeStore
            rt = RuntimeStore(Layout(eng.base)).load()
            gate = rt.human_gate["gate_id"]
            HumanEventStore(eng.layout.human_inbox_dir).append("HUMAN_CHANGES_REQUESTED", message="r1", gate_id=gate)
            await wait_until(lambda: event_names(eng).count("WAIT_HUMAN") >= 2)
            rt2 = RuntimeStore(Layout(eng.base)).load()
            gate2 = rt2.human_gate["gate_id"]
            HumanEventStore(eng.layout.human_inbox_dir).append("HUMAN_CHANGES_REQUESTED", message="r2", gate_id=gate2)
            await wait_until(lambda: event_names(eng).count("WAIT_HUMAN") >= 3)
            rt3 = RuntimeStore(Layout(eng.base)).load()
            gate3 = rt3.human_gate["gate_id"]
            HumanEventStore(eng.layout.human_inbox_dir).append("HUMAN_APPROVED", gate_id=gate3)
        rc = run_engine(engine, control)
        assert rc == 0
        assert runner.calls == [1,2,3,4]
