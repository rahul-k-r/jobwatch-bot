"""Repost signals. A hit lowers alert priority; it never suppresses the alert."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta

from jobwatch.db import DB
from jobwatch.models import Job

_REPOST_TEXT = re.compile(
    r"\bre-?post(ed|ing)?\b|\bpreviously\s+(posted|applied)\b|\bif\s+you\s+(have\s+)?(already|previously)\s+applied\b",
    re.I,
)


@dataclass
class RepostRules:
    created_gap_days: float = 14
    similarity: float = 0.9
    lookback_days: int = 90


def repost_reason(job: Job, db: DB, now: datetime, rules: RepostRules) -> str | None:
    if job.created_at and job.posted_at:
        gap = (job.posted_at - job.created_at).total_seconds() / 86400
        if gap > rules.created_gap_days:
            return f"requisition created {gap:.0f}d before this posting"
    # Newly seen but published long ago: re-listed or un-hidden, not a fresh opening.
    if job.posted_at and (age := (now - job.posted_at).days) > rules.created_gap_days:
        return f"first published {age}d ago"
    if job.req_id and db.req_id_seen(job.company, job.req_id, exclude_key=job.key):
        return "same requisition as an earlier posting"
    if job.description:
        if m := _REPOST_TEXT.search(job.description):
            return f"posting says '{m.group(0)}'"
        since = now - timedelta(days=rules.lookback_days)
        mine = _shingles(job.description)
        for other, seen in db.descriptions_for_title(job.company, job.title, since, exclude_key=job.key):
            if _jaccard(mine, _shingles(other)) >= rules.similarity:
                # Age tells a same-day batch of identical openings (0d) apart from a real repost.
                return f"near-identical to a posting seen {(now - seen).days}d ago"
    return None


def _shingles(text: str, n: int = 5) -> set[int]:
    words = re.findall(r"[a-z0-9]+", text.lower())
    return {hash(tuple(words[i : i + n])) for i in range(max(len(words) - n + 1, 1))}


def _jaccard(a: set[int], b: set[int]) -> float:
    return len(a & b) / len(a | b) if a and b else 0.0
