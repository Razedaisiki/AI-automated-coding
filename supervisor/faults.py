"""Fault injection for M11 crash testing — test-only, no-op in production."""
from typing import Protocol


class FaultInjector(Protocol):
    def hit(self, point: str) -> None:
        ...


class NullFaultInjector:
    def hit(self, point: str) -> None:
        return


class CrashAt:
    """Raise injected crash at a specific point."""
    def __init__(self, point: str, exc_type=RuntimeError):
        self.point = point
        self.exc_type = exc_type

    def hit(self, point: str) -> None:
        if point == self.point:
            raise self.exc_type(f"injected crash at {point}")
