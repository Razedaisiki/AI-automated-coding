"""M9 — Parent Delivery Contract (AgentState validation + policy)."""
from supervisor.models import AgentState, AgentStateError

class TestWaitCIValidation:
    def test_valid_wait_ci_with_matching_git(self):
        AgentState.from_dict({"schema_version":1,"status":"WAIT_CI","checkpoint_seq":1,"updated_at":"2026-08-18T06:00:00Z","ci":{"sha":"a"*40},"git":{"head":"a"*40,"pushed_head":"a"*40}})

    def test_wait_ci_bare_is_ok(self):
        # legacy fake without sha — permissive
        AgentState.from_dict({"schema_version":1,"status":"WAIT_CI","checkpoint_seq":1,"updated_at":"2026-08-18T06:00:00Z"})

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
