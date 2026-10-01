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
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from jobwatch.models import Job

log = logging.getLogger(__name__)


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
        payload = {**job.to_dict(), "key": job.key, "queued_at": datetime.now(UTC).isoformat()}
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
