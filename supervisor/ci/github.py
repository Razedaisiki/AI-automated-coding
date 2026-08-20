"""GitHubCiProvider — gh CLI backed CI provider.

All gh / git subprocess logic lives here; engine.py must never call gh directly.
"""

from __future__ import annotations

import json
import re
import subprocess
import urllib.parse
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from supervisor.ci.base import CiMaterial, CiObservation
from supervisor.events import now_iso
from supervisor.models import CiStatus
from supervisor.storage import atomic_write_json, write_text_atomic

_LOG_SIZE_CAP = 2 * 1024 * 1024  # 2 MiB
_GH_TIMEOUT = 15


# ---------------------------------------------------------------------------
# git helpers
# ---------------------------------------------------------------------------

def _git(repo: Path, *args: str) -> Optional[subprocess.CompletedProcess]:
    try:
        return subprocess.run(
            ["git", *args],
            cwd=str(repo),
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None


def _parse_github_remote(url: str) -> Optional[Tuple[str, str]]:
    """Parse a git remote URL into (owner, repo) if it points at github.com."""
    url = url.strip()
    if not url:
        return None
    # strip trailing .git
    if url.endswith(".git"):
        url = url[:-4]
    # SSH scp-like: git@github.com:owner/repo
    m = re.match(r"^[\w\d\-_\.]+@github\.com:(.+/.+)$", url)
    if m:
        path = m.group(1)
        parts = path.strip("/").split("/")
        if len(parts) >= 2 and "github.com" in url:
            owner, repo = parts[0], parts[1]
            if owner and repo:
                return owner, repo
        return None
    # URL forms: https://github.com/owner/repo , ssh://git@github.com/owner/repo etc.
    try:
        parsed = urllib.parse.urlparse(url)
        host = parsed.hostname or ""
        # handle ssh://git@github.com/... where urlparse netloc includes user
        if "github.com" not in host and "github.com" not in url:
            return None
        if "github.com" not in host:
            # for scp-like we already handled; this is fallback string check
            if "github.com" not in url:
                return None
        path = parsed.path.strip("/")
        if not path and ":" in url and "://" not in url:
            # should have been caught above
            return None
        parts = path.split("/")
        if len(parts) >= 2:
            owner, repo = parts[0], parts[1]
            if owner and repo:
                # ensure host really is github
                if "github.com" in (host or "") or "github.com" in url:
                    return owner, repo
    except Exception:
        return None
    return None


def _resolve_github_repo(repo: Path) -> Optional[Tuple[str, str]]:
    """Return (owner, repo) for a github remote, or None if not github."""
    repo = Path(repo)
    # Try origin first
    r = _git(repo, "remote", "get-url", "origin")
    if r is not None and r.returncode == 0 and r.stdout.strip():
        parsed = _parse_github_remote(r.stdout.strip())
        if parsed is not None:
            return parsed
    # Fallback: enumerate all remotes sorted deterministically
    r = _git(repo, "remote")
    if r is not None and r.returncode == 0 and r.stdout.strip():
        remotes = sorted(line.strip() for line in r.stdout.splitlines() if line.strip())
        for name in remotes:
            if name == "origin":
                continue  # already checked
            rr = _git(repo, "remote", "get-url", name)
            if rr is not None and rr.returncode == 0 and rr.stdout.strip():
                parsed = _parse_github_remote(rr.stdout.strip())
                if parsed is not None:
                    return parsed
    return None


# ---------------------------------------------------------------------------
# gh helpers
# ---------------------------------------------------------------------------

def _run_gh(args: List[str], cwd: Path, timeout: int = _GH_TIMEOUT) -> Tuple[Optional[subprocess.CompletedProcess], Optional[str]]:
    """Run gh CLI.

    Returns (result, error_kind) where error_kind is None on success (even if
    returncode != 0 — caller inspects), "not_installed" if gh binary missing,
    "timeout" on timeout, "error" on other OSError.
    """
    try:
        result = subprocess.run(
            args,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return result, None
    except FileNotFoundError:
        return None, "not_installed"
    except subprocess.TimeoutExpired:
        return None, "timeout"
    except (OSError, subprocess.SubprocessError):
        return None, "error"


def _map_runs_to_status(runs: List[dict]) -> Tuple[CiStatus, Optional[str], Optional[str]]:
    """Map list of gh run objects to unified CiStatus.

    Returns (status, provider_run_id, provider_url) picking a representative
    run (failure > cancelled > pending > success).
    """
    if not runs:
        return CiStatus.NOT_FOUND, None, None

    # Normalise: filter to dicts
    runs = [r for r in runs if isinstance(r, dict)]
    if not runs:
        return CiStatus.NOT_FOUND, None, None

    def _run_id(r: dict) -> Optional[str]:
        for k in ("databaseId", "databaseID", "id"):
            v = r.get(k)
            if v is not None:
                return str(v)
        return None

    def _url(r: dict) -> Optional[str]:
        for k in ("url", "html_url", "htmlUrl"):
            v = r.get(k)
            if v:
                return str(v)
        return None

    # Classify each run
    has_pending = False
    failure_run: Optional[dict] = None
    cancelled_run: Optional[dict] = None
    pending_run: Optional[dict] = None
    success_run: Optional[dict] = None

    for r in runs:
        status = str(r.get("status") or "").lower()
        conclusion = str(r.get("conclusion") or "").lower() if r.get("conclusion") is not None else ""

        # GitHub API check-runs use different field names: status/conclusion same
        # gh run list uses status=completed|in_progress|queued and conclusion
        is_completed = status == "completed"
        if not is_completed:
            # Any non-completed is pending (queued, in_progress, waiting, requested, pending)
            has_pending = True
            if pending_run is None:
                pending_run = r
            # But a completed run with failure should still outrank pending? For CI,
            # if any job already failed we want FAILURE even if others pending.
            # So we continue to check other runs.

        if is_completed:
            if conclusion in ("failure", "failed", "timed_out", "timedout", "action_required"):
                if failure_run is None:
                    failure_run = r
            elif conclusion in ("cancelled", "canceled", "skipped"):
                if cancelled_run is None:
                    cancelled_run = r
            elif conclusion in ("success", "successful", "neutral", "skipped"):
                # neutral/skipped considered success for our purposes unless failure present
                if success_run is None and conclusion == "success":
                    success_run = r
                elif success_run is None:
                    success_run = r
            elif conclusion in ("", "null", "none") and not status:
                # unknown
                pass
            else:
                # e.g. "startup_failure" is failure-like
                if "fail" in conclusion:
                    if failure_run is None:
                        failure_run = r

    # Priority: failure > cancelled > pending > success
    if failure_run is not None:
        return CiStatus.FAILURE, _run_id(failure_run), _url(failure_run)
    if cancelled_run is not None:
        return CiStatus.CANCELLED, _run_id(cancelled_run), _url(cancelled_run)
    if has_pending:
        # pick a pending run for id/url if available
        rep = pending_run or runs[0]
        return CiStatus.PENDING, _run_id(rep), _url(rep)
    if success_run is not None:
        # verify all are success; if all completed and at least one success, success
        # if there were no failures/cancelled/pending, we can return success
        return CiStatus.SUCCESS, _run_id(success_run), _url(success_run)
    # Fallback: if runs exist but none matched above, treat as pending
    return CiStatus.PENDING, _run_id(runs[0]), _url(runs[0])


def _truncate_text(text: str, cap: int = _LOG_SIZE_CAP) -> str:
    encoded = text.encode("utf-8")
    if len(encoded) <= cap:
        return text
    keep = cap - 200
    truncated = encoded[:keep].decode("utf-8", errors="replace")
    # avoid cutting mid-line
    truncated = truncated.rsplit("\n", 1)[0] + "\n"
    truncated += "... [truncated at 2MB cap] ...\n"
    return truncated


# ---------------------------------------------------------------------------
# Provider
# ---------------------------------------------------------------------------

class GitHubCiProvider:
    """CI provider backed by the gh CLI.

    Isolation: all subprocess/gh logic lives here. Engine only calls the two
    async methods.
    """

    def __init__(self) -> None:
        self._last_observations: Dict[str, CiObservation] = {}

    async def get_status(self, *, repo: Path, sha: str) -> CiObservation:
        repo = Path(repo)
        if not isinstance(sha, str) or not sha:
            raise ValueError("sha must be non-empty string")
        observed_at = now_iso()

        github_repo = _resolve_github_repo(repo)
        if github_repo is None:
            obs = CiObservation(
                provider="github",
                sha=sha,
                status=CiStatus.NOT_FOUND,
                observed_at=observed_at,
                raw={"reason": "not_github_repo_or_no_remote"},
            )
            self._last_observations[sha] = obs
            return obs

        owner, repo_name = github_repo

        # Probe gh existence first (cheap, also handles not-installed case)
        # We do it implicitly via the main command's FileNotFoundError, but
        # an explicit check gives clearer raw.
        # Try gh run list path
        result, err = _run_gh(
            ["gh", "run", "list", "--commit", sha, "--json", "status,conclusion,databaseId,url", "--limit", "20"],
            cwd=repo,
        )
        if err == "not_installed":
            obs = CiObservation(
                provider="github",
                sha=sha,
                status=CiStatus.ERROR,
                observed_at=observed_at,
                raw={"error": "gh_not_installed"},
            )
            self._last_observations[sha] = obs
            return obs
        if err == "timeout":
            obs = CiObservation(
                provider="github",
                sha=sha,
                status=CiStatus.ERROR,
                observed_at=observed_at,
                raw={"error": "gh_timeout", "command": "gh run list"},
            )
            self._last_observations[sha] = obs
            return obs
        if err == "error":
            obs = CiObservation(
                provider="github",
                sha=sha,
                status=CiStatus.ERROR,
                observed_at=observed_at,
                raw={"error": "gh_spawn_error", "command": "gh run list"},
            )
            self._last_observations[sha] = obs
            return obs

        assert result is not None

        # gh run list returns JSON array on success; non-zero may mean no runs or api error
        # Distinguish: empty success vs error
        if result.returncode == 0:
            try:
                data = json.loads(result.stdout) if result.stdout.strip() else []
            except json.JSONDecodeError as exc:
                obs = CiObservation(
                    provider="github",
                    sha=sha,
                    status=CiStatus.ERROR,
                    observed_at=observed_at,
                    raw={"error": "gh_json_parse_error", "detail": str(exc), "stdout": result.stdout[:500]},
                )
                self._last_observations[sha] = obs
                return obs
            # data should be list
            if isinstance(data, dict):
                # Some gh versions wrap; handle
                data = [data]
            if not isinstance(data, list):
                obs = CiObservation(
                    provider="github",
                    sha=sha,
                    status=CiStatus.ERROR,
                    observed_at=observed_at,
                    raw={"error": "unexpected_gh_json_shape", "data": str(data)[:500]},
                )
                self._last_observations[sha] = obs
                return obs
            status, run_id, url = _map_runs_to_status(data)
            obs = CiObservation(
                provider="github",
                sha=sha,
                status=status,
                observed_at=observed_at,
                provider_run_id=run_id,
                provider_url=url,
                raw={"runs": data[:5], "total": len(data)},  # cap raw size
            )
            # NOT_FOUND from empty list is already handled in _map
            self._last_observations[sha] = obs
            return obs

        # Non-zero exit: try gh api check-runs as fallback before giving ERROR
        # Common case: repo has no runs yet -> gh may exit 0 with empty; non-zero is api error
        api_result, api_err = _run_gh(
            ["gh", "api", f"repos/{owner}/{repo_name}/commits/{sha}/check-runs", "--jq", ".check_runs"],
            cwd=repo,
        )
        if api_err == "not_installed":
            obs = CiObservation(
                provider="github",
                sha=sha,
                status=CiStatus.ERROR,
                observed_at=observed_at,
                raw={"error": "gh_not_installed", "fallback": "check-runs"},
            )
            self._last_observations[sha] = obs
            return obs
        if api_err in ("timeout", "error"):
            obs = CiObservation(
                provider="github",
                sha=sha,
                status=CiStatus.ERROR,
                observed_at=observed_at,
                raw={"error": f"gh_{api_err}", "command": "gh api check-runs", "gh_run_list_stderr": result.stderr[:500]},
            )
            self._last_observations[sha] = obs
            return obs
        assert api_result is not None
        if api_result.returncode == 0:
            try:
                # --jq already extracts array; else parse full object
                text = api_result.stdout.strip()
                if not text:
                    data: List[dict] = []
                elif text.startswith("["):
                    data = json.loads(text)
                elif text.startswith("{"):
                    obj = json.loads(text)
                    # when without --jq
                    data = obj.get("check_runs", []) if isinstance(obj, dict) else []
                else:
                    # jq output may be line-delimited json
                    data = []
                    for line in text.splitlines():
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            data.append(json.loads(line))
                        except json.JSONDecodeError:
                            continue
            except json.JSONDecodeError as exc:
                obs = CiObservation(
                    provider="github",
                    sha=sha,
                    status=CiStatus.ERROR,
                    observed_at=observed_at,
                    raw={"error": "gh_api_json_error", "detail": str(exc)},
                )
                self._last_observations[sha] = obs
                return obs
            status, run_id, url = _map_runs_to_status(data)
            obs = CiObservation(
                provider="github",
                sha=sha,
                status=status,
                observed_at=observed_at,
                provider_run_id=run_id,
                provider_url=url,
                raw={"check_runs": data[:5], "total": len(data)},
            )
            self._last_observations[sha] = obs
            return obs

        # Both paths failed -> ERROR, but detect "not found" style 404 as NOT_FOUND
        stderr = (result.stderr + " " + api_result.stderr).lower()
        if "404" in stderr or "not found" in stderr:
            obs = CiObservation(
                provider="github",
                sha=sha,
                status=CiStatus.NOT_FOUND,
                observed_at=observed_at,
                raw={"reason": "github_404", "stderr": (result.stderr[:500] + " " + api_result.stderr[:500]).strip()},
            )
            self._last_observations[sha] = obs
            return obs

        obs = CiObservation(
            provider="github",
            sha=sha,
            status=CiStatus.ERROR,
            observed_at=observed_at,
            raw={
                "error": "gh_api_error",
                "gh_run_list_stderr": result.stderr[:500],
                "gh_api_stderr": api_result.stderr[:500],
                "gh_run_list_exit": result.returncode,
                "gh_api_exit": api_result.returncode,
            },
        )
        self._last_observations[sha] = obs
        return obs

    async def collect_failure(self, *, repo: Path, sha: str, destination: Path) -> CiMaterial:
        repo = Path(repo)
        destination = Path(destination)
        destination.mkdir(parents=True, exist_ok=True)
        logs_dir = destination / "logs"
        logs_dir.mkdir(parents=True, exist_ok=True)

        # Prefer last observation; else synthesize FAILURE
        obs = self._last_observations.get(sha)
        if obs is None:
            # Try to fetch status to populate provider_run_id
            try:
                obs = await self.get_status(repo=repo, sha=sha)
                # If still not failure-like, synthesize a FAILURE observation for material purposes
                if obs.status not in (CiStatus.FAILURE, CiStatus.ERROR, CiStatus.CANCELLED):
                    obs = CiObservation(
                        provider="github",
                        sha=sha,
                        status=CiStatus.FAILURE,
                        observed_at=now_iso(),
                        provider_run_id=obs.provider_run_id,
                        provider_url=obs.provider_url,
                        raw={"synthetic": True, "original_status": obs.status.value},
                    )
                    self._last_observations[sha] = obs
            except Exception:
                obs = CiObservation(
                    provider="github",
                    sha=sha,
                    status=CiStatus.FAILURE,
                    observed_at=now_iso(),
                    raw={"synthetic": True, "reason": "get_status_failed"},
                )
                self._last_observations[sha] = obs

        # Ensure observation status is failure-like for material; keep original if already
        if obs.status == CiStatus.SUCCESS:
            # Collecting failure for a success doesn't make sense but handle gracefully
            pass

        run_id = obs.provider_run_id
        github_repo = _resolve_github_repo(repo)

        log_text: Optional[str] = None
        jobs_data: Optional[List[dict]] = None

        if run_id is not None:
            # Try gh run view --log
            result, err = _run_gh(["gh", "run", "view", str(run_id), "--log"], cwd=repo, timeout=30)
            if err is None and result is not None and result.returncode == 0 and result.stdout:
                log_text = result.stdout
            else:
                # Try failed log flag if available
                result2, err2 = _run_gh(["gh", "run", "view", str(run_id), "--log-failed"], cwd=repo, timeout=30)
                if err2 is None and result2 is not None and result2.returncode == 0 and result2.stdout:
                    log_text = result2.stdout

            # Try to fetch jobs for failed-jobs.json
            jobs_result, jobs_err = _run_gh(
                ["gh", "run", "view", str(run_id), "--json", "jobs"],
                cwd=repo,
            )
            if jobs_err is None and jobs_result is not None and jobs_result.returncode == 0 and jobs_result.stdout.strip():
                try:
                    obj = json.loads(jobs_result.stdout)
                    if isinstance(obj, dict) and "jobs" in obj:
                        jobs_data = obj["jobs"] if isinstance(obj["jobs"], list) else []
                    elif isinstance(obj, list):
                        jobs_data = obj
                except json.JSONDecodeError:
                    jobs_data = None
            # Fallback: gh api jobs
            if jobs_data is None and github_repo is not None and run_id is not None:
                owner, repo_name = github_repo
                api_jobs, api_err = _run_gh(
                    ["gh", "api", f"repos/{owner}/{repo_name}/actions/runs/{run_id}/jobs"],
                    cwd=repo,
                )
                if api_err is None and api_jobs is not None and api_jobs.returncode == 0 and api_jobs.stdout.strip():
                    try:
                        obj = json.loads(api_jobs.stdout)
                        if isinstance(obj, dict) and "jobs" in obj:
                            jobs_data = obj["jobs"]
                    except json.JSONDecodeError:
                        pass

        if log_text is None:
            # Synthetic log when gh unavailable or no run id
            reason = "no_provider_run_id" if run_id is None else "gh_log_unavailable"
            log_text = (
                f"GitHub CI log unavailable ({reason})\n"
                f"sha: {sha}\n"
                f"provider: github\n"
                f"provider_run_id: {run_id or 'n/a'}\n"
                f"provider_url: {obs.provider_url or 'n/a'}\n"
                f"status: {obs.status.value if isinstance(obs.status, CiStatus) else obs.status}\n"
                f"observed_at: {obs.observed_at}\n"
                "---\n"
                "No log content retrieved. This may mean gh is not installed, not authenticated,\n"
                "the run has no logs yet, or the repository is not a GitHub repository.\n"
            )
            if github_repo is None:
                log_text += "Repository does not appear to be a GitHub repository.\n"

        # Size cap + safe write
        log_text = _truncate_text(log_text, _LOG_SIZE_CAP)

        # -- observation.json -------------------------------------------------
        observation_data = {
            "provider": obs.provider,
            "sha": obs.sha,
            "status": obs.status.value if isinstance(obs.status, CiStatus) else str(obs.status),
            "observed_at": obs.observed_at,
            "provider_run_id": obs.provider_run_id,
            "provider_url": obs.provider_url,
            "raw": obs.raw,
        }
        atomic_write_json(destination / "observation.json", observation_data)

        # -- summary.txt ------------------------------------------------------
        summary_text = (
            f"GitHub CI failure material\n"
            f"sha: {sha}\n"
            f"status: {obs.status.value if isinstance(obs.status, CiStatus) else obs.status}\n"
            f"observed_at: {obs.observed_at}\n"
            f"provider: {obs.provider}\n"
            f"provider_run_id: {obs.provider_run_id or 'n/a'}\n"
            f"provider_url: {obs.provider_url or 'n/a'}\n"
        )
        write_text_atomic(destination / "summary.txt", summary_text)

        # -- failed-jobs.json -------------------------------------------------
        if jobs_data is None:
            # Synthesize minimal failed jobs from observation
            jobs_data = [
                {
                    "name": "build",
                    "status": "completed",
                    "conclusion": "failure",
                    "sha": sha,
                    "provider_run_id": run_id,
                    "provider_url": obs.provider_url,
                }
            ]
        else:
            # Ensure each entry is json-serializable; truncate large logs
            # Keep at most 50 jobs to bound file size
            jobs_data = jobs_data[:50]

        atomic_write_json(destination / "failed-jobs.json", jobs_data)

        # -- logs/build.log ---------------------------------------------------
        write_text_atomic(logs_dir / "build.log", log_text)

        files: List[Path] = [
            destination / "observation.json",
            destination / "summary.txt",
            destination / "failed-jobs.json",
            logs_dir / "build.log",
        ]
        return CiMaterial(sha=sha, observation=obs, files=files)
