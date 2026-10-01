from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from datetime import datetime
from enum import IntEnum
from typing import Any


@dataclass
class Job:
    company: str
    source_id: str  # the platform's posting ID; changes on some reposts
    title: str
    url: str
    locations: list[str] = field(default_factory=list)
    req_id: str | None = None  # requisition ID; stable across reposts where the platform exposes it
    posted_at: datetime | None = None
    created_at: datetime | None = None  # requisition creation, when distinct from posting
    description: str | None = None  # plain text, filled by Source.enrich
    repost_reason: str | None = None
    classification: dict[str, Any] | None = None
    ai_instructions: list[str] = field(default_factory=list)  # sentences in the posting addressing AI tools
    ai_policy: list[str] = field(default_factory=list)  # e.g. "AI-generated applications will be rejected"
    canaries: list[str] = field(default_factory=list)  # phrases those instructions try to plant in a CV

    @property
    def key(self) -> str:
        return f"{self.company}:{self.source_id}"

    def to_dict(self) -> dict[str, Any]:
        d = dataclasses.asdict(self)
        for k in ("posted_at", "created_at"):
            d[k] = d[k].isoformat() if d[k] else None
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Job:
        d = dict(d)
        for k in ("posted_at", "created_at"):
            d[k] = datetime.fromisoformat(d[k]) if d[k] else None
        return cls(**d)


class Priority(IntEnum):
    # Matches ntfy's 1-5 scale so a notifier can pass it through.
    LOW = 2
    DEFAULT = 3
    HIGH = 4


@dataclass
class Alert:
    job: Job
    priority: Priority
    labels: list[str] = field(default_factory=list)
