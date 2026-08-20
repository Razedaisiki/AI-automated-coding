"""CI provider package — re-exports for ergonomic imports."""

from supervisor.ci.base import CiMaterial, CiObservation, CiProvider
from supervisor.ci.fake import FakeCiProvider
from supervisor.models import CiStatus

# Lazy import for GitHubCiProvider so importing supervisor.ci does not require gh.
def __getattr__(name: str):  # PEP 562
    if name == "GitHubCiProvider":
        from supervisor.ci.github import GitHubCiProvider as _G

        return _G
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "CiProvider",
    "CiObservation",
    "CiMaterial",
    "CiStatus",
    "FakeCiProvider",
    "GitHubCiProvider",
]
