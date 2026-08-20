"""CiProvider protocol and data models."""

from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Protocol

from supervisor.models import CiStatus


@dataclass
class CiObservation:
    provider: str
    sha: str
    status: CiStatus
    observed_at: str
    provider_run_id: Optional[str] = None
    provider_url: Optional[str] = None
    raw: Optional[dict] = None


@dataclass
class CiMaterial:
    sha: str
    observation: CiObservation
    files: List[Path]


class CiProvider(Protocol):
    async def get_status(self, *, repo: Path, sha: str) -> CiObservation: ...

    async def collect_failure(self, *, repo: Path, sha: str, destination: Path) -> CiMaterial: ...
