"""Scripted Parent runners for M9/M10 E2E — real filesystem/Git, zero LLM."""
import asyncio
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from supervisor.models import AgentState, AgentStatus, ParentResult


def _rev_parse(repo: Path) -> str:
    return subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(repo), capture_output=True, text=True, check=True).stdout.strip()


class SimpleDeliveryRunner:
    """One-shot delivery: modify file, commit, push, WAIT_CI(exact SHA)."""

    def __init__(self, layout, target_file: str = "pkg/app.py", new_content=None, branch: str = "master"):
        self.layout = layout
        self.target_file = target_file
        self.new_content = new_content or "def add(a,b): return a+b\n# delivered\n"
        self.branch = branch
        self.calls = []
        self.prompts = []
        self._seq = 0

    async def run(self, *, repo, prompt, activation_id, timeout_seconds, run_dir, on_start=None, activation_token=None, lease_fd=None):
        repo = Path(repo)
        run_dir = Path(run_dir)
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "prompt.txt").write_text(prompt, encoding="utf-8")
        (run_dir / "stdout.log").write_text("scripted delivery\n", encoding="utf-8")
        (run_dir / "stderr.log").write_text("", encoding="utf-8")
        self.calls.append(activation_id)
        self.prompts.append(prompt)
        if on_start:
            on_start(999999, "fake-start")
        # real filesystem work
        target = repo / self.target_file
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(self.new_content, encoding="utf-8")
        # ensure validation passes (pytest -q)
        subprocess.run(["git", "add", "-A"], cwd=str(repo), check=True)
        subprocess.run(["git", "commit", "-q", "-m", f"delivery {activation_id}"], cwd=str(repo), check=True)
        subprocess.run(["git", "push", "-q", "origin", "HEAD"], cwd=str(repo), check=True)
        sha = _rev_parse(repo)
        self._seq += 1
        st = AgentState(schema_version=1, status=AgentStatus.WAIT_CI, checkpoint_seq=self._seq, updated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"), ci={"sha": sha}, git={"head": sha, "pushed_head": sha})
        self.layout.agent_state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.layout.agent_state_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(st.to_dict()), encoding="utf-8")
        tmp.replace(self.layout.agent_state_path)
        return ParentResult(activation_id=activation_id, exit_code=0, timed_out=False, started_at="2026-08-18T06:00:00Z", ended_at="2026-08-18T06:00:00Z", duration_seconds=0, stdout_path=str(run_dir / "stdout.log"), stderr_path=str(run_dir / "stderr.log"), run_dir=str(run_dir), pid=999999, process_start_id="fake-start")


class RepairBeforeCommitRunner:
    """First content fails validation, repairs before commit."""

    def __init__(self, layout, buggy: str, fixed: str, target_file: str = "pkg/app.py"):
        self.layout = layout
        self.buggy = buggy
        self.fixed = fixed
        self.target_file = target_file
        self.calls = []
        self.prompts = []
        self._seq = 0
        self.push_shas = []

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
        target = repo / self.target_file
        # write buggy, validate fails
        target.write_text(self.buggy, encoding="utf-8")
        bad = subprocess.run(["python3", "-m", "pytest", "-q"], cwd=str(repo), capture_output=True)
        assert bad.returncode != 0, "buggy content should fail validation"
        # ensure buggy not pushed: remote should not contain uncommitted state
        # repair
        target.write_text(self.fixed, encoding="utf-8")
        good = subprocess.run(["python3", "-m", "pytest", "-q"], cwd=str(repo), capture_output=True)
        assert good.returncode == 0, "fixed content should pass"
        subprocess.run(["git", "add", "-A"], cwd=str(repo), check=True)
        subprocess.run(["git", "commit", "-q", "-m", f"repair {activation_id}"], cwd=str(repo), check=True)
        subprocess.run(["git", "push", "-q", "origin", "HEAD"], cwd=str(repo), check=True)
        sha = _rev_parse(repo)
        self.push_shas.append(sha)
        self._seq += 1
        st = AgentState(schema_version=1, status=AgentStatus.WAIT_CI, checkpoint_seq=self._seq, updated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"), ci={"sha": sha}, git={"head": sha, "pushed_head": sha})
        self.layout.agent_state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.layout.agent_state_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(st.to_dict()), encoding="utf-8")
        tmp.replace(self.layout.agent_state_path)
        return ParentResult(activation_id=activation_id, exit_code=0, timed_out=False, started_at="2026-08-18T06:00:00Z", ended_at="2026-08-18T06:00:00Z", duration_seconds=0, stdout_path=str(run_dir / "stdout.log"), stderr_path=str(run_dir / "stderr.log"), run_dir=str(run_dir), pid=999999, process_start_id="fake-start")
