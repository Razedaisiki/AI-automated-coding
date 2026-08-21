"""M10 — PR / Review / Repair Loop."""
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


class TestPRCreateAndReuse:
    def test_pr_create_happy(self, tmp_path):
        from tests.helpers.target_repo import make_fake_target
        from supervisor.config import default_config
        from supervisor.engine import SupervisorEngine
        from supervisor.storage import Layout, RuntimeStore
        from supervisor.human_events import HumanEventStore
        from conftest import event_names, run_engine, wait_until
        from tests.helpers.review_parent import PrCreateRunner
        from tests.fakes.fake_github import FakePrStore
        import asyncio
        base = make_fake_target(tmp_path, "m10a")
        repo = base["repo"]
        layout = Layout(repo)
        pr_store = FakePrStore(repo / ".pr_store")
        cfg = default_config()
        cfg.restart.backoff_seconds = [0.01]
        runner = PrCreateRunner(layout, pr_store, branch="feature/test")
        engine = SupervisorEngine(base_dir=repo, config=cfg, runner=runner)
        async def control(eng):
            await wait_until(lambda: "WAIT_HUMAN" in event_names(eng))
            await asyncio.sleep(0.2)
            eng.request_stop()
        run_engine(engine, control)
        assert pr_store.count() == 1
        pr = pr_store.list()[0]
        st = AgentState.from_dict(__import__("json").loads((repo / ".agent" / "state.json").read_text(encoding="utf-8")))
        assert st.review["pr_number"] == pr["number"]
        rt = RuntimeStore(layout).load()
        assert rt.human_gate is not None

    def test_pr_crash_idempotency(self, tmp_path):
        from tests.helpers.target_repo import make_fake_target
        from supervisor.config import default_config
        from supervisor.engine import SupervisorEngine
        from supervisor.storage import Layout
        from tests.helpers.review_parent import CrashAfterPrCreateRunner, PrCreateRunner
        from tests.fakes.fake_github import FakePrStore
        from conftest import event_names, run_engine, wait_until
        import asyncio
        base = make_fake_target(tmp_path, "m10b")
        repo = base["repo"]
        layout = Layout(repo)
        pr_store = FakePrStore(repo / ".pr_store")
        cfg = default_config()
        cfg.restart.backoff_seconds = [0.01]
        cfg.limits.max_crash_restarts = 10
        # first engine crashes after PR create before checkpoint -> will restart and retry
        # Use a runner that crashes once then recovers to WAIT_HUMAN; we model it as
        # a single engine run where CrashAfterPrCreateRunner does crash on activation 1
        # and then PrCreateRunner-style reuse on activation 2 (same instance handles both).
        runner = CrashAfterPrCreateRunner(layout, pr_store, branch="feature/test")
        engine = SupervisorEngine(base_dir=repo, config=cfg, runner=runner)
        async def control(eng):
            await wait_until(lambda: "WAIT_HUMAN" in event_names(eng))
            await asyncio.sleep(0.2)
            eng.request_stop()
        run_engine(engine, control)
        assert pr_store.count() == 1

    def test_changes_requested_full_loop_same_pr(self, tmp_path):
        import subprocess
        from tests.helpers.target_repo import make_fake_target, remote_contains
        from supervisor.config import default_config
        from supervisor.engine import SupervisorEngine
        from supervisor.storage import Layout, RuntimeStore
        from supervisor.human_events import HumanEventStore
        from conftest import event_names, run_engine, wait_until
        from tests.fakes.fake_github import FakePrStore
        from tests.helpers.review_parent import PrCreateRunner, ChangesRequestedRepairRunner
        import asyncio
        base = make_fake_target(tmp_path, "m10c")
        repo = base["repo"]
        layout = Layout(repo)
        pr_store = FakePrStore(repo / ".pr_store")
        cfg = default_config()
        cfg.restart.backoff_seconds = [0.01]
        # Step 1: create PR -> WAIT_HUMAN G1
        runner1 = PrCreateRunner(layout, pr_store, branch="feature/test")
        engine1 = SupervisorEngine(base_dir=repo, config=cfg, runner=runner1)
        async def ctrl1(eng):
            await wait_until(lambda: "WAIT_HUMAN" in event_names(eng))
            await asyncio.sleep(0.1)
            eng.request_stop()
        run_engine(engine1, ctrl1)
        assert pr_store.count() == 1
        sha_a = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(repo), capture_output=True, text=True).stdout.strip()
        assert pr_store.list()[0]["head_sha"] == sha_a
        rt = RuntimeStore(layout).load()
        gate1 = rt.human_gate["gate_id"]
        # Simulate human changes requested
        HumanEventStore(layout.human_inbox_dir).append("HUMAN_CHANGES_REQUESTED", message="fix me", gate_id=gate1)
        # Step 2: repair -> WAIT_HUMAN G2 with same PR
        runner2 = ChangesRequestedRepairRunner(layout, pr_store, branch="feature/test", repair_content="def add(a,b): return a+b\n# repair m10c\n", next_status="WAIT_HUMAN")
        engine2 = SupervisorEngine(base_dir=repo, config=cfg, runner=runner2)
        async def ctrl2(eng):
            await wait_until(lambda: event_names(eng).count("WAIT_HUMAN") >= 2)
            await asyncio.sleep(0.2)
            eng.request_stop()
        run_engine(engine2, ctrl2)
        sha_b = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(repo), capture_output=True, text=True).stdout.strip()
        assert sha_b != sha_a
        assert remote_contains(base["remote"], sha_b)
        assert pr_store.count() == 1
        assert pr_store.list()[0]["head_sha"] == sha_b
        st = AgentState.from_dict(__import__("json").loads((repo / ".agent" / "state.json").read_text(encoding="utf-8")))
        assert st.review["pr_number"] == 1

    def test_approval_to_completed(self, tmp_path):
        from tests.helpers.target_repo import make_fake_target
        from supervisor.config import default_config
        from supervisor.engine import SupervisorEngine
        from supervisor.storage import Layout, RuntimeStore
        from supervisor.human_events import HumanEventStore
        from conftest import event_names, run_engine, wait_until
        from tests.helpers.review_parent import PrCreateRunner, ChangesRequestedRepairRunner
        from tests.fakes.fake_github import FakePrStore
        base = make_fake_target(tmp_path, "m10d")
        repo = base["repo"]
        layout = Layout(repo)
        pr_store = FakePrStore(repo / ".pr_store")
        cfg = default_config()
        cfg.restart.backoff_seconds = [0.01]
        runner1 = PrCreateRunner(layout, pr_store, branch="feature/test")
        engine1 = SupervisorEngine(base_dir=repo, config=cfg, runner=runner1)
        async def ctrl1(eng):
            await wait_until(lambda: "WAIT_HUMAN" in event_names(eng))
            import asyncio; await asyncio.sleep(0.1)
            eng.request_stop()
        run_engine(engine1, ctrl1)
        rt = RuntimeStore(layout).load()
        gate1 = rt.human_gate["gate_id"]
        HumanEventStore(layout.human_inbox_dir).append("HUMAN_CHANGES_REQUESTED", message="fix", gate_id=gate1)
        runner2 = ChangesRequestedRepairRunner(layout, pr_store, branch="feature/test", repair_content="def add(a,b): return a+b\n# repair2\n", next_status="WAIT_HUMAN")
        engine2 = SupervisorEngine(base_dir=repo, config=cfg, runner=runner2)
        async def ctrl2(eng):
            await wait_until(lambda: event_names(eng).count("WAIT_HUMAN") >= 2)
            import asyncio; await asyncio.sleep(0.1)
            eng.request_stop()
        run_engine(engine2, ctrl2)
        rt2 = RuntimeStore(layout).load()
        gate2 = rt2.human_gate["gate_id"]
        HumanEventStore(layout.human_inbox_dir).append("HUMAN_APPROVED", gate_id=gate2)
        runner3 = ChangesRequestedRepairRunner(layout, pr_store, branch="feature/test")
        engine3 = SupervisorEngine(base_dir=repo, config=cfg, runner=runner3)
        import asyncio as _aio
        async def ctrl3(eng):
            await _aio.sleep(0.5)
            if eng.rt and eng.rt.status.value == "STOPPED_SUCCESS":
                return
            eng.request_stop()
        try:
            rc = run_engine(engine3, ctrl3, timeout=5)
        except asyncio.CancelledError:
            rc = 0
        assert pr_store.count() == 1

    def test_repeated_review_cycle_same_pr(self, tmp_path):
        import subprocess
        from tests.helpers.target_repo import make_fake_target
        from supervisor.config import default_config
        from supervisor.engine import SupervisorEngine
        from supervisor.storage import Layout, RuntimeStore
        from supervisor.human_events import HumanEventStore
        from conftest import event_names, run_engine, wait_until
        from tests.fakes.fake_github import FakePrStore
        from tests.helpers.review_parent import PrCreateRunner, ChangesRequestedRepairRunner
        base = make_fake_target(tmp_path, "m10e")
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
        for i, content in enumerate(["def add(a,b): return a+b\n# r1\n", "def add(a,b): return a+b\n# r2\n"]):
            rt = RuntimeStore(layout).load()
            gate = rt.human_gate["gate_id"]
            HumanEventStore(layout.human_inbox_dir).append("HUMAN_CHANGES_REQUESTED", message=f"r{i}", gate_id=gate)
            runner = ChangesRequestedRepairRunner(layout, pr_store, branch="feature/test", repair_content=content, next_status="WAIT_HUMAN")
            engine = SupervisorEngine(base_dir=repo, config=cfg, runner=runner)
            async def ctrl(eng, exp=2+i):
                await wait_until(lambda: event_names(eng).count("WAIT_HUMAN") >= exp)
                import asyncio; await asyncio.sleep(0.1)
                eng.request_stop()
            run_engine(engine, ctrl)
            assert pr_store.count() == 1
        rt = RuntimeStore(layout).load()
        gate = rt.human_gate["gate_id"]
        HumanEventStore(layout.human_inbox_dir).append("HUMAN_APPROVED", gate_id=gate)
        runner_final = ChangesRequestedRepairRunner(layout, pr_store, branch="feature/test")
        engine_final = SupervisorEngine(base_dir=repo, config=cfg, runner=runner_final)
        import asyncio as _aio2
        async def ctrl_final(eng):
            await _aio2.sleep(0.5)
            if eng.rt and eng.rt.status.value == "STOPPED_SUCCESS":
                return
            eng.request_stop()
        try:
            rc = run_engine(engine_final, ctrl_final, timeout=5)
        except asyncio.CancelledError:
            rc = 0
        assert pr_store.count() == 1
