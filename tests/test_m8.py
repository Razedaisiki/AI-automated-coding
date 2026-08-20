"""M8 — Durable Human Gate."""
import json
from pathlib import Path

from supervisor.human_events import HumanEventStore
from supervisor.config import default_config
from supervisor.engine import SupervisorEngine
from supervisor.storage import Layout, RuntimeStore
from supervisor.models import SupervisorStatus, StopReason
from supervisor.events import EventLog

from conftest import FakeParentRunner, Step, StepScript, event_names, run_engine, wait_until


class TestHumanEventStore:
    def test_append_and_list(self, tmp_path):
        store = HumanEventStore(tmp_path / "human")
        e1 = store.append("HUMAN_APPROVED", message="ok")
        assert e1.event_id == "human-000001"
        assert e1.status == "PENDING"
        e2 = store.append("HUMAN_CHANGES_REQUESTED", message="fix")
        assert e2.event_id == "human-000002"
        assert len(store.list_all()) == 2
        assert store.next_pending().event_id == "human-000001"

    def test_invalid_type(self, tmp_path):
        store = HumanEventStore(tmp_path / "human")
        try:
            store.append("NOPE")
            assert False, "should raise"
        except ValueError:
            pass

    def test_file_attachment(self, tmp_path):
        f = tmp_path / "feedback.md"
        f.write_text("look here", encoding="utf-8")
        store = HumanEventStore(tmp_path / "human")
        e = store.append("HUMAN_CHANGES_REQUESTED", file=f)
        assert e.attachment_path is not None
        assert "human-000001" in e.attachment_path

    def test_mark_delivering_delivered(self, tmp_path):
        store = HumanEventStore(tmp_path / "human")
        e = store.append("HUMAN_APPROVED")
        store.mark_delivering(e.event_id)
        assert store.get(e.event_id).status == "DELIVERING"
        assert store.next_pending().event_id == e.event_id  # still pending (DELIVERING counts)
        store.mark_delivered(e.event_id)
        assert store.get(e.event_id).status == "DELIVERED"
        assert store.next_pending() is None

class TestWaitHumanEngine:
    def test_wait_human_approved(self, tmp_repo):
        cfg = default_config()
        cfg.restart.backoff_seconds = [0.01]
        runner = FakeParentRunner([Step(status="WAIT_HUMAN"), Step(status="COMPLETED")], Layout(tmp_repo))
        engine = SupervisorEngine(base_dir=tmp_repo, config=cfg, runner=runner)
        async def control(eng):
            await wait_until(lambda: "WAIT_HUMAN" in event_names(eng))
            HumanEventStore(eng.layout.human_inbox_dir).append("HUMAN_APPROVED", message="go")
        rc = run_engine(engine, control)
        assert rc == 0
        assert runner.calls == [1,2]
        assert "HUMAN_EVENT_RECEIVED" in event_names(engine)

    def test_wait_human_changes_requested(self, tmp_repo):
        cfg = default_config()
        cfg.restart.backoff_seconds = [0.01]
        runner = FakeParentRunner([Step(status="WAIT_HUMAN"), Step(status="COMPLETED")], Layout(tmp_repo))
        engine = SupervisorEngine(base_dir=tmp_repo, config=cfg, runner=runner)
        async def control(eng):
            await wait_until(lambda: "WAIT_HUMAN" in event_names(eng))
            HumanEventStore(eng.layout.human_inbox_dir).append("HUMAN_CHANGES_REQUESTED", message="compat")
        rc = run_engine(engine, control)
        assert rc == 0
        assert runner.calls == [1,2]

    def test_resume_cli_message_and_file(self, tmp_repo):
        from supervisor.cli import main
        fb = tmp_repo / "fb.md"
        fb.write_text("feedback", encoding="utf-8")
        rc = main(["resume", str(tmp_repo), "--event", "HUMAN_CHANGES_REQUESTED", "--message", "keep api", "--file", str(fb)])
        assert rc == 0
        store = HumanEventStore(Layout(tmp_repo).human_inbox_dir)
        e = store.list_all()[0]
        assert e.message == "keep api"
        assert e.attachment_path is not None

    def test_pending_delivery_crash_recovery(self, tmp_repo):
        # event left in DELIVERING should be re-delivered after restart
        cfg = default_config()
        cfg.restart.backoff_seconds = [0.01]
        store = HumanEventStore(Layout(tmp_repo).human_inbox_dir)
        e = store.append("HUMAN_APPROVED")
        store.mark_delivering(e.event_id)  # simulate crash after DELIVERING before DELIVERED
        # Now restart engine waiting for human: should still see it
        runner = FakeParentRunner([Step(status="WAIT_HUMAN"), Step(status="COMPLETED")], Layout(tmp_repo))
        engine = SupervisorEngine(base_dir=tmp_repo, config=cfg, runner=runner)
        rc = run_engine(engine)
        assert rc == 0
        assert runner.calls == [1,2]
