"""FakeCiProvider for deterministic tests."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Union

from supervisor.ci.base import CiMaterial, CiObservation
from supervisor.events import now_iso
from supervisor.models import CiStatus
from supervisor.storage import atomic_write_json, write_text_atomic

_LOG_SIZE_CAP = 2 * 1024 * 1024  # 2 MiB


def _normalize_status(entry) -> CiStatus:
    """Extract CiStatus from entry which may be CiStatus or (CiStatus, float)."""
    # unwrap tuple/list wrapper e.g. (CiStatus.PENDING, 0.05)
    if isinstance(entry, (tuple, list)):
        if not entry:
            raise ValueError("empty tuple/list script entry")
        # first element is the status; second (if exists) is delay which we ignore
        entry = entry[0]
    if isinstance(entry, CiStatus):
        return entry
    if isinstance(entry, str):
        # allow string values like "PENDING"
        try:
            return CiStatus(entry)
        except ValueError as exc:
            raise ValueError(f"invalid CiStatus string {entry!r}") from exc
    raise TypeError(f"invalid script entry {entry!r} (expected CiStatus or (CiStatus, float))")


class FakeCiProvider:
    """Deterministic fake CI provider.

    Constructor forms::

        FakeCiProvider([CiStatus.PENDING, CiStatus.SUCCESS])
        FakeCiProvider(script=[CiStatus.PENDING, CiStatus.SUCCESS])
        FakeCiProvider(statuses=[CiStatus.PENDING, CiStatus.SUCCESS])
        FakeCiProvider({"abc123": [CiStatus.PENDING, CiStatus.FAILURE], "_default": [CiStatus.SUCCESS]})
        FakeCiProvider(script={"abc123": [...]})
        # tuple form with optional delay (delay is ignored / not slept):
        FakeCiProvider([(CiStatus.PENDING, 0.01), CiStatus.SUCCESS])

    Script handling:
    - If a plain list is given it is used for every SHA (per-SHA cursor, shared script).
    - If a dict is given it is a per-SHA mapping keyed by exact SHA or SHA prefix.
      The longest matching prefix wins; ``_default`` is the fallback.
    - ``statuses=`` is an alias for ``script=`` (list form).
    """

    def __init__(
        self,
        script: Union[List[Union[CiStatus, Tuple[CiStatus, float]]], Dict[str, List[Union[CiStatus, Tuple[CiStatus, float]]]], None] = None,
        statuses: Optional[List[Union[CiStatus, Tuple[CiStatus, float]]]] = None,
    ) -> None:
        # ``statuses`` is alias for ``script`` when list form.
        if script is None and statuses is not None:
            script = statuses

        # default when nothing provided
        if script is None:
            script = [CiStatus.SUCCESS]

        self.calls_count: Dict[str, int] = {}
        self._cursors: Dict[str, int] = {}
        self._last_observations: Dict[str, CiObservation] = {}

        self._is_dict: bool = isinstance(script, dict)
        self._default_script: List[CiStatus] = []
        self._per_sha_scripts: Dict[str, List[CiStatus]] = {}

        if self._is_dict:
            assert isinstance(script, dict)
            for key, lst in script.items():
                if not isinstance(key, str) or not key:
                    raise ValueError(f"script dict key must be non-empty string, got {key!r}")
                if not isinstance(lst, list):
                    raise TypeError(f"script for {key!r} must be list, got {type(lst).__name__}")
                self._per_sha_scripts[key] = [_normalize_status(e) for e in lst]
            # ensure _default exists as fallback if caller used list-like dict without it?
            # not required – _resolve falls back to empty which yields NOT_FOUND
        else:
            assert isinstance(script, list)
            self._default_script = [_normalize_status(e) for e in script]
            self._per_sha_scripts = {}

    # ------------------------------------------------------------------ helpers
    def _resolve_script(self, sha: str) -> List[CiStatus]:
        if not self._is_dict:
            return self._default_script
        # exact match first
        if sha in self._per_sha_scripts:
            return self._per_sha_scripts[sha]
        # longest prefix match where key is prefix of sha
        best_key: Optional[str] = None
        best_len = -1
        for key in self._per_sha_scripts:
            if key == "_default":
                continue
            if sha.startswith(key) and len(key) > best_len:
                best_key = key
                best_len = len(key)
        if best_key is not None:
            return self._per_sha_scripts[best_key]
        if "_default" in self._per_sha_scripts:
            return self._per_sha_scripts["_default"]
        return []

    # ------------------------------------------------------------------ public helpers
    def set_per_sha_script(self, sha: str, script: List[Union[CiStatus, Tuple[CiStatus, float]]]) -> None:
        """Override script for a specific SHA (exact match). Resets its cursor."""
        if not isinstance(sha, str) or not sha:
            raise ValueError("sha must be non-empty string")
        if not isinstance(script, list):
            raise TypeError("script must be a list")
        normalized = [_normalize_status(e) for e in script]
        if self._is_dict:
            self._per_sha_scripts[sha] = normalized
        else:
            # promote list-mode to dict-mode to allow per-SHA overrides
            self._per_sha_scripts = {"_default": list(self._default_script), sha: normalized}
            self._is_dict = True
        # reset cursor for this sha so next get_status starts at 0
        self._cursors.pop(sha, None)

    # ------------------------------------------------------------------ CiProvider API
    async def get_status(self, *, repo: Path, sha: str) -> CiObservation:
        _ = Path(repo)  # keep signature, repo unused for fake but validated
        if not isinstance(sha, str) or not sha:
            raise ValueError("sha must be non-empty string")

        self.calls_count[sha] = self.calls_count.get(sha, 0) + 1

        script = self._resolve_script(sha)
        idx = self._cursors.get(sha, 0)

        if not script:
            status = CiStatus.NOT_FOUND
        elif idx < len(script):
            status = script[idx]
            self._cursors[sha] = idx + 1
        else:
            status = script[-1]
            # keep cursor at end (reuse last)

        observed_at = now_iso()

        # Provide deterministic fake provider metadata for non-NOT_FOUND
        provider_run_id: Optional[str] = None
        provider_url: Optional[str] = None
        if status not in (CiStatus.NOT_FOUND,):
            # fake run id ties to sha + call count for debuggability
            provider_run_id = f"fake-{sha[:7]}-{self.calls_count[sha]}"
            provider_url = f"https://example.com/fake/run/{provider_run_id}"

        raw: Optional[dict] = {"script_index": idx, "calls": self.calls_count[sha]}
        # mark if we reused last (idx beyond script)
        if script and idx >= len(script):
            raw["reused_last"] = True

        obs = CiObservation(
            provider="fake",
            sha=sha,
            status=status,
            observed_at=observed_at,
            provider_run_id=provider_run_id,
            provider_url=provider_url,
            raw=raw,
        )
        self._last_observations[sha] = obs
        return obs

    async def collect_failure(self, *, repo: Path, sha: str, destination: Path) -> CiMaterial:
        _ = Path(repo)
        destination = Path(destination)
        destination.mkdir(parents=True, exist_ok=True)
        logs_dir = destination / "logs"
        logs_dir.mkdir(parents=True, exist_ok=True)

        # Use last observation if we have one, otherwise synthesise a FAILURE
        obs = self._last_observations.get(sha)
        if obs is None:
            obs = CiObservation(
                provider="fake",
                sha=sha,
                status=CiStatus.FAILURE,
                observed_at=now_iso(),
                provider_run_id=f"fake-{sha[:7]}-0",
                provider_url=f"https://example.com/fake/run/fake-{sha[:7]}-0",
                raw={"synthetic": True},
            )
            self._last_observations[sha] = obs

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
            f"Fake CI failure material\n"
            f"sha: {sha}\n"
            f"status: {obs.status.value if isinstance(obs.status, CiStatus) else obs.status}\n"
            f"observed_at: {obs.observed_at}\n"
            f"provider: {obs.provider}\n"
            f"provider_run_id: {obs.provider_run_id or 'n/a'}\n"
            f"provider_url: {obs.provider_url or 'n/a'}\n"
        )
        write_text_atomic(destination / "summary.txt", summary_text)

        # -- failed-jobs.json -------------------------------------------------
        failed_jobs = [
            {
                "name": "build",
                "status": "completed",
                "conclusion": "failure",
                "sha": sha,
                "provider_run_id": obs.provider_run_id,
            },
            {
                "name": "test",
                "status": "completed",
                "conclusion": "failure",
                "sha": sha,
                "provider_run_id": obs.provider_run_id,
            },
        ]
        atomic_write_json(destination / "failed-jobs.json", failed_jobs)

        # -- logs/build.log ---------------------------------------------------
        log_text = (
            f"Fake CI log for sha {sha}\n"
            f"Provider: fake\n"
            f"Status: {obs.status.value if isinstance(obs.status, CiStatus) else obs.status}\n"
            f"Observed at: {obs.observed_at}\n"
            f"Provider run id: {obs.provider_run_id or 'n/a'}\n"
            "---\n"
            "ERROR: Build failed: simulated failure\n"
            "Stacktrace:\n"
            "  at fake.module.build (fake.py:42)\n"
            "  at fake.module.test (fake.py:100)\n"
            "--- End of fake log ---\n"
        )
        # Size cap 2 MiB – truncate with marker if needed
        encoded = log_text.encode("utf-8")
        if len(encoded) > _LOG_SIZE_CAP:
            # keep head, add truncation marker; ensure we don't split utf-8 incorrectly
            keep = _LOG_SIZE_CAP - 200
            truncated = encoded[:keep].decode("utf-8", errors="replace")
            # avoid cutting in middle of line – find last newline
            truncated = truncated.rsplit("\n", 1)[0] + "\n"
            truncated += "... [truncated at 2MB cap] ...\n"
            log_text = truncated

        write_text_atomic(logs_dir / "build.log", log_text)

        files: List[Path] = [
            destination / "observation.json",
            destination / "summary.txt",
            destination / "failed-jobs.json",
            logs_dir / "build.log",
        ]
        return CiMaterial(sha=sha, observation=obs, files=files)
