"""File-based handoff to an external worker (e.g. one that builds a tailored resume per job).

jobwatch writes accepted jobs to <dir>/inbox/; the worker writes a verdict per job to
<dir>/outbox/, which jobwatch turns into a "Resume ready" / "Resume not built" push. Files (not a
direct call) keep the two decoupled and make every step survive a crash or restart of either side.
"""

from __future__ import annotations

import json
import logging
import os
import re
from datetime import UTC, datetime, time, timedelta
from pathlib import Path
from typing import Any

from jobwatch.models import Job

log = logging.getLogger(__name__)

# A date-only posting can go up a whole day after its date, plus up to ~8h if the site's date is US-local.
_DAY_SLACK = timedelta(hours=32)


def _date_only(t: datetime) -> bool:
    # Amazon, IBM, Workday, Workable and Eightfold give a day, stored as midnight UTC.
    return t.utcoffset() == timedelta(0) and t.time() == time(0)


def age_from(job: Job) -> datetime | None:
    """The time a consumer should measure the posting's age from.

    A real timestamp is used as is. A date-only one would make every posting look hours old by
    afternoon, so it is narrowed by the previous successful poll: the job wasn't there then, so it
    can't be older than that. Unless the date itself proves it older (it surfaced late), or no earlier
    poll is known (first run), in which case the date is all there is.
    """
    t = job.posted_at
    if t is None or not _date_only(t) or job.prev_poll_at is None:
        return t
    return min(job.prev_poll_at, t + _DAY_SLACK)


class Handoff:
    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.inbox, self.outbox = self.root / "inbox", self.root / "outbox"
        self.sent = self.outbox / "sent"
        for d in (self.inbox, self.outbox, self.sent):
            d.mkdir(parents=True, exist_ok=True)

    def queue(self, job: Job) -> Path:
        name = re.sub(r"[^A-Za-z0-9._-]+", "-", job.key).strip("-")[:120] + ".json"
        path = self.inbox / name
        basis = age_from(job)
        payload = {**job.to_dict(), "key": job.key, "queued_at": datetime.now(UTC).isoformat(),
                   "age_from": basis.isoformat() if basis else None}
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        os.replace(tmp, path)  # the worker never sees a half-written file
        return path

    def collect(self) -> list[tuple[Path, dict[str, Any]]]:
        results = []
        for path in sorted(self.outbox.glob("*.json")):
            try:
                results.append((path, json.loads(path.read_text(encoding="utf-8"))))
            except (OSError, ValueError) as e:
                log.warning("skipping unreadable handoff result %s: %r", path.name, e)
        return results

    def mark_sent(self, path: Path) -> None:
        os.replace(path, self.sent / path.name)
