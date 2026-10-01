from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable
from datetime import datetime

from jobwatch.models import Job

# seeded:   existed when we first looked at the company; never alerted
# filtered: rejected by a rule or the classifier (reason says why)
# pending:  passed the rules, awaiting classification and/or a successful alert
# notified: alert delivered
_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    key         TEXT PRIMARY KEY,
    company     TEXT NOT NULL,
    req_id      TEXT,
    norm_title  TEXT NOT NULL,
    status      TEXT NOT NULL,
    reason      TEXT,
    first_seen  TEXT NOT NULL,
    notified_at TEXT,
    data        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS jobs_company_req ON jobs (company, req_id);
CREATE INDEX IF NOT EXISTS jobs_company_title ON jobs (company, norm_title);
CREATE INDEX IF NOT EXISTS jobs_status ON jobs (status);
CREATE TABLE IF NOT EXISTS llm_usage (day TEXT PRIMARY KEY, requests INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS seeded_companies (company TEXT PRIMARY KEY, seeded_at TEXT NOT NULL);
"""


def norm_title(title: str) -> str:
    return " ".join(title.lower().split())


class DB:
    def __init__(self, path: str):
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        # WAL: one persistent -wal file instead of creating/deleting a -journal per commit.
        # NORMAL is durable against crashes; a power cut can lose the last commits (worst case a re-sent alert).
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.executescript(_SCHEMA)

    def close(self) -> None:
        self.conn.close()

    # Seeding is only complete once marked, so a seed that dies halfway is redone
    # rather than leaving older unseen postings to be alerted as new.
    def is_seeded(self, company: str) -> bool:
        row = self.conn.execute("SELECT 1 FROM seeded_companies WHERE company = ?", (company,)).fetchone()
        return row is not None

    def mark_seeded(self, company: str, now: datetime) -> None:
        self.conn.execute("INSERT OR IGNORE INTO seeded_companies VALUES (?, ?)", (company, now.isoformat()))
        self.conn.commit()

    def known_keys(self, keys: Iterable[str]) -> set[str]:
        keys = list(keys)
        if not keys:
            return set()
        marks = ",".join("?" * len(keys))
        return {r[0] for r in self.conn.execute(f"SELECT key FROM jobs WHERE key IN ({marks})", keys)}

    def insert(self, job: Job, status: str, now: datetime, reason: str | None = None) -> None:
        self.insert_many([job], status, now, reason)

    def insert_many(self, jobs: list[Job], status: str, now: datetime, reason: str | None = None) -> None:
        """One transaction for the batch (a seed page is up to hundreds of jobs)."""
        self.conn.executemany(
            "INSERT OR IGNORE INTO jobs (key, company, req_id, norm_title, status, reason, first_seen, data)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [(j.key, j.company, j.req_id, norm_title(j.title), status, reason, now.isoformat(),
              json.dumps(j.to_dict())) for j in jobs],
        )
        self.conn.commit()

    def update(self, job: Job, status: str, reason: str | None = None, notified_at: datetime | None = None) -> None:
        self.conn.execute(
            "UPDATE jobs SET status = ?, reason = ?, notified_at = ?, data = ? WHERE key = ?",
            (status, reason, notified_at.isoformat() if notified_at else None, json.dumps(job.to_dict()), job.key),
        )
        self.conn.commit()

    def pending(self) -> list[Job]:
        rows = self.conn.execute("SELECT data FROM jobs WHERE status = 'pending' ORDER BY first_seen")
        return [Job.from_dict(json.loads(r[0])) for r in rows]

    def req_id_seen(self, company: str, req_id: str, exclude_key: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM jobs WHERE company = ? AND req_id = ? AND key != ? LIMIT 1", (company, req_id, exclude_key)
        ).fetchone()
        return row is not None

    def descriptions_for_title(
        self, company: str, title: str, since: datetime, exclude_key: str
    ) -> list[tuple[str, datetime]]:
        """(description, first_seen) of the company's same-title postings seen since `since`."""
        rows = self.conn.execute(
            "SELECT data, first_seen FROM jobs WHERE company = ? AND norm_title = ? AND first_seen >= ? AND key != ?",
            (company, norm_title(title), since.isoformat(), exclude_key),
        )
        return [(d, datetime.fromisoformat(r[1])) for r in rows if (d := json.loads(r[0]).get("description"))]

    def llm_requests(self, day: str) -> int:
        row = self.conn.execute("SELECT requests FROM llm_usage WHERE day = ?", (day,)).fetchone()
        return row[0] if row else 0

    def add_llm_request(self, day: str) -> None:
        self.conn.execute(
            "INSERT INTO llm_usage (day, requests) VALUES (?, 1)"
            " ON CONFLICT(day) DO UPDATE SET requests = requests + 1",
            (day,),
        )
        self.conn.commit()
