"""Durable Human Gate — append-only event store (M8)."""

import json
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

from .storage import atomic_write_json

VALID_HUMAN_EVENTS = {"HUMAN_APPROVED", "HUMAN_CHANGES_REQUESTED"}


@dataclass
class HumanEvent:
    event_id: str  # human-000001
    event_type: str  # HUMAN_APPROVED | HUMAN_CHANGES_REQUESTED
    created_at: str
    message: Optional[str] = None
    attachment_path: Optional[str] = None  # repo-relative path
    status: str = "PENDING"  # PENDING | DELIVERING | DELIVERED

    def to_dict(self):
        return asdict(self)

    @classmethod
    def from_dict(cls, raw):
        return cls(
            event_id=raw.get("event_id", ""),
            event_type=raw.get("event_type", ""),
            created_at=raw.get("created_at", ""),
            message=raw.get("message"),
            attachment_path=raw.get("attachment_path"),
            status=raw.get("status", "PENDING"),
        )


class HumanEventStore:
    def __init__(self, inbox_dir: Path):
        self.inbox_dir = Path(inbox_dir)  # should be layout.human_inbox_dir

    def _next_id(self) -> str:
        existing = sorted(self.inbox_dir.glob("human-*.json")) if self.inbox_dir.exists() else []
        max_n = 0
        for p in existing:
            m = re.match(r"human-(\d+)", p.stem)
            if m:
                max_n = max(max_n, int(m.group(1)))
        return f"human-{max_n+1:06d}"

    def append(self, event_type: str, message: Optional[str] = None, file: Optional[Path] = None) -> HumanEvent:
        if event_type not in VALID_HUMAN_EVENTS:
            raise ValueError(f"invalid human event type {event_type!r}")
        self.inbox_dir.mkdir(parents=True, exist_ok=True)
        event_id = self._next_id()
        created_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        attachment_path = None
        if file is not None:
            file = Path(file)
            if not file.exists():
                raise FileNotFoundError(f"attachment not found: {file}")
            dest_dir = self.inbox_dir / "attachments" / event_id
            dest_dir.mkdir(parents=True, exist_ok=True)
            dest = dest_dir / file.name
            data = file.read_bytes()
            if len(data) > 1 * 1024 * 1024:
                data = data[: 1 * 1024 * 1024] + b"\n... [truncated at 1MB cap] ...\n"
            dest.write_bytes(data)
            # repo-relative like .supervisor/inbox/human/attachments/<event_id>/<filename>
            attachment_path = f".supervisor/inbox/human/attachments/{event_id}/{file.name}"
        event = HumanEvent(
            event_id=event_id,
            event_type=event_type,
            created_at=created_at,
            message=message,
            attachment_path=attachment_path,
            status="PENDING",
        )
        atomic_write_json(self.inbox_dir / f"{event_id}.json", event.to_dict())
        return event

    def list_all(self) -> List[HumanEvent]:
        if not self.inbox_dir.exists():
            return []
        events = []
        for p in sorted(self.inbox_dir.glob("human-*.json")):
            try:
                raw = json.loads(p.read_text(encoding="utf-8"))
                events.append(HumanEvent.from_dict(raw))
            except Exception:
                continue
        return events

    def list_pending(self) -> List[HumanEvent]:
        return [e for e in self.list_all() if e.status in ("PENDING", "DELIVERING")]

    def next_pending(self) -> Optional[HumanEvent]:
        for e in sorted(self.list_all(), key=lambda x: x.event_id):
            if e.status in ("PENDING", "DELIVERING"):
                return e
        return None

    def mark_delivering(self, event_id: str):
        p = self.inbox_dir / f"{event_id}.json"
        if not p.exists():
            raise FileNotFoundError(event_id)
        raw = json.loads(p.read_text(encoding="utf-8"))
        raw["status"] = "DELIVERING"
        atomic_write_json(p, raw)

    def mark_delivered(self, event_id: str):
        p = self.inbox_dir / f"{event_id}.json"
        if not p.exists():
            raise FileNotFoundError(event_id)
        raw = json.loads(p.read_text(encoding="utf-8"))
        raw["status"] = "DELIVERED"
        atomic_write_json(p, raw)

    def get(self, event_id: str) -> Optional[HumanEvent]:
        p = self.inbox_dir / f"{event_id}.json"
        if not p.exists():
            return None
        return HumanEvent.from_dict(json.loads(p.read_text(encoding="utf-8")))
