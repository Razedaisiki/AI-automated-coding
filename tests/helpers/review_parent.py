"""Review/PR Parent runners — file-backed PR store, real Git, lookup/reuse."""
import asyncio
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from supervisor.models import AgentState, AgentStatus, ParentResult
from tests.fakes.fake_github import FakePrStore


def _rev(repo: Path) -> str:
    return subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(repo), capture_output=True, text=True, check=True).stdout.strip()


class PrCreateRunner:
    """CI_SUCCESS -> lookup branch -> create PR -> WAIT_HUMAN with review."""

    def __init__(self, layout, pr_store: FakePrStore, branch: str = "feature/test"):
        self.layout = layout
        self.pr_store = pr_store
        self.branch = branch
        self.calls = []
        self.prompts = []
        self._seq = 0

    async def run(self, *, repo, prompt, activation_id, timeout_seconds, run_dir, on_start=None, activation_token=None, lease_fd=None):
        repo = Path(repo)
        run_dir = Path(run_dir)
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "prompt.txt").write_text(prompt, encoding="utf-8")
        (run_dir / "stdout.log").write_text("", encoding="utf-8")
        (run_dir / "stderr.log").write_text("", encoding="utf-8")
        self.calls.append(activation_id)
        self.prompts.append(prompt)
        if on_start:
            on_start(999999, "fake-start")
        # lookup or create — idempotent
        existing = self.pr_store.find_by_branch(self.branch)
        head = _rev(repo)
        if existing is None:
            pr = self.pr_store.create(branch=self.branch, head_sha=head, title="delivery", body="pr body")
        else:
            pr = self.pr_store.update(existing["number"], head_sha=head)
        self._seq += 1
        st = AgentState(schema_version=1, status=AgentStatus.WAIT_HUMAN, checkpoint_seq=self._seq, updated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"), review={"provider": "github", "pr_number": pr["number"], "pr_url": f"https://example.com/pr/{pr['number']}", "head_sha": pr["head_sha"]})
        self.layout.agent_state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.layout.agent_state_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(st.to_dict()), encoding="utf-8")
        tmp.replace(self.layout.agent_state_path)
        return ParentResult(activation_id=activation_id, exit_code=0, timed_out=False, started_at="2026-08-18T06:00:00Z", ended_at="2026-08-18T06:00:00Z", duration_seconds=0, stdout_path=str(run_dir / "stdout.log"), stderr_path=str(run_dir / "stderr.log"), run_dir=str(run_dir), pid=999999, process_start_id="fake-start")


class CrashAfterPrCreateRunner(PrCreateRunner):
    """Create PR then crash before checkpoint (delete state)."""

    async def run(self, *, repo, prompt, activation_id, timeout_seconds, run_dir, on_start=None, activation_token=None, lease_fd=None):
        if activation_id == 1:
            repo = Path(repo)
            run_dir = Path(run_dir)
            run_dir.mkdir(parents=True, exist_ok=True)
            (run_dir / "prompt.txt").write_text(prompt, encoding="utf-8")
            (run_dir / "stdout.log").write_text("", encoding="utf-8")
            (run_dir / "stderr.log").write_text("", encoding="utf-8")
            self.calls.append(activation_id)
            self.prompts.append(prompt)
            if on_start:
                on_start(999999, "fake-start")
            head = _rev(repo)
            # create but don't write agent state — simulate crash before checkpoint
            self.pr_store.create(branch=self.branch, head_sha=head, title="delivery", body="first")
            # delete state if exists, exit non-zero
            if self.layout.agent_state_path.exists():
                self.layout.agent_state_path.unlink()
            return ParentResult(activation_id=activation_id, exit_code=1, timed_out=False, started_at="2026-08-18T06:00:00Z", ended_at="2026-08-18T06:00:00Z", duration_seconds=0, stdout_path=str(run_dir / "stdout.log"), stderr_path=str(run_dir / "stderr.log"), run_dir=str(run_dir), pid=999999, process_start_id="fake-start")
        return await super().run(repo=repo, prompt=prompt, activation_id=activation_id, timeout_seconds=timeout_seconds, run_dir=run_dir, on_start=on_start, activation_token=activation_token, lease_fd=lease_fd)


class ChangesRequestedRepairRunner:
    """After HUMAN_CHANGES_REQUESTED, repair file, commit+push, update same PR, WAIT_CI or WAIT_HUMAN."""

    def __init__(self, layout, pr_store: FakePrStore, branch: str = "feature/test", repair_content: str = "def add(a,b): return a+b\n# repair\n", next_status: str = "WAIT_HUMAN"):
        self.layout = layout
        self.pr_store = pr_store
        self.branch = branch
        self.repair_content = repair_content
        self.next_status = next_status
        self.calls = []
        self.prompts = []
        self._seq = 0

    async def run(self, *, repo, prompt, activation_id, timeout_seconds, run_dir, on_start=None, activation_token=None, lease_fd=None):
        repo = Path(repo)
        run_dir = Path(run_dir)
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "prompt.txt").write_text(prompt, encoding="utf-8")
        (run_dir / "stdout.log").write_text("", encoding="utf-8")
        (run_dir / "stderr.log").write_text("", encoding="utf-8")
        self.calls.append(activation_id)
        self.prompts.append(prompt)
        if on_start:
            on_start(999999, "fake-start")
        is_human_event = "HUMAN_CHANGES_REQUESTED" in prompt or "HUMAN_APPROVED" in prompt or "Human event" in prompt
        if is_human_event and "HUMAN_CHANGES_REQUESTED" in prompt:
            # do repair
            target = repo / "pkg" / "app.py"
            target.write_text(self.repair_content, encoding="utf-8")
            subprocess.run(["git", "add", "-A"], cwd=str(repo), check=True)
            subprocess.run(["git", "commit", "-q", "-m", f"repair {activation_id}"], cwd=str(repo), check=True)
            subprocess.run(["git", "push", "-q", "origin", "HEAD"], cwd=str(repo), check=True)
            head = _rev(repo)
            pr = self.pr_store.find_by_branch(self.branch)
            if pr:
                self.pr_store.update(pr["number"], head_sha=head)
                pr = self.pr_store.read(pr["number"])
            else:
                pr = self.pr_store.create(branch=self.branch, head_sha=head)
            if self.next_status == "WAIT_CI":
                self._seq += 1
                st = AgentState(schema_version=1, status=AgentStatus.WAIT_CI, checkpoint_seq=self._seq, updated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"), ci={"sha": head}, git={"head": head, "pushed_head": head})
            else:
                self._seq += 1
                st = AgentState(schema_version=1, status=AgentStatus.WAIT_HUMAN, checkpoint_seq=self._seq, updated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"), review={"provider": "github", "pr_number": pr["number"], "pr_url": f"https://example.com/pr/{pr['number']}", "head_sha": pr["head_sha"]})
            self.layout.agent_state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.layout.agent_state_path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(st.to_dict()), encoding="utf-8")
            tmp.replace(self.layout.agent_state_path)
            return ParentResult(activation_id=activation_id, exit_code=0, timed_out=False, started_at="2026-08-18T06:00:00Z", ended_at="2026-08-18T06:00:00Z", duration_seconds=0, stdout_path=str(run_dir / "stdout.log"), stderr_path=str(run_dir / "stderr.log"), run_dir=str(run_dir), pid=999999, process_start_id="fake-start")
        if is_human_event and "HUMAN_APPROVED" in prompt:
            self._seq += 1
            st = AgentState(schema_version=1, status=AgentStatus.COMPLETED, checkpoint_seq=self._seq, updated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"))
            self.layout.agent_state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.layout.agent_state_path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(st.to_dict()), encoding="utf-8")
            tmp.replace(self.layout.agent_state_path)
            return ParentResult(activation_id=activation_id, exit_code=0, timed_out=False, started_at="2026-08-18T06:00:00Z", ended_at="2026-08-18T06:00:00Z", duration_seconds=0, stdout_path=str(run_dir / "stdout.log"), stderr_path=str(run_dir / "stderr.log"), run_dir=str(run_dir), pid=999999, process_start_id="fake-start")
        # default: reuse PrCreate
        head = _rev(repo)
        existing = self.pr_store.find_by_branch(self.branch)
        if existing is None:
            pr = self.pr_store.create(branch=self.branch, head_sha=head)
        else:
            pr = self.pr_store.update(existing["number"], head_sha=head)
        self._seq += 1
        st = AgentState(schema_version=1, status=AgentStatus.WAIT_HUMAN, checkpoint_seq=self._seq, updated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"), review={"provider": "github", "pr_number": pr["number"], "pr_url": f"https://example.com/pr/{pr['number']}", "head_sha": pr["head_sha"]})
        self.layout.agent_state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.layout.agent_state_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(st.to_dict()), encoding="utf-8")
        tmp.replace(self.layout.agent_state_path)
        return ParentResult(activation_id=activation_id, exit_code=0, timed_out=False, started_at="2026-08-18T06:00:00Z", ended_at="2026-08-18T06:00:00Z", duration_seconds=0, stdout_path=str(run_dir / "stdout.log"), stderr_path=str(run_dir / "stderr.log"), run_dir=str(run_dir), pid=999999, process_start_id="fake-start")
