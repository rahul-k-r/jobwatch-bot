from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import yaml

from jobwatch.classifier import ClassifierConfig
from jobwatch.filters import TitleRules
from jobwatch.repost import RepostRules
from jobwatch.sources import Source, build_source


@dataclass
class NtfyConfig:
    server: str
    topic: str  # new postings and operational notices
    token: str | None
    resume_topic: str | None = None  # "Resume ready / not built"; falls back to `topic`
    low_topic: str | None = None  # reposts and other low-priority alerts; falls back to `topic`


@dataclass
class OffPeak:
    """Slower polling outside the hours when postings actually go up."""

    interval: int = 1800
    timezone: str = "America/Los_Angeles"
    busy_hours: tuple[int, int] = (6, 21)  # [start, end) local hour
    weekdays_only: bool = True

    def is_busy(self, now: datetime) -> bool:
        local = now.astimezone(ZoneInfo(self.timezone))
        start, end = self.busy_hours
        return start <= local.hour < end and (not self.weekdays_only or local.weekday() < 5)


@dataclass
class Config:
    sources: list[Source]
    title_rules: TitleRules
    company_rules: dict[str, TitleRules] = field(default_factory=dict)
    source_intervals: dict[str, int] = field(default_factory=dict)  # per-company minimum seconds between polls
    snoozes: dict[str, date] = field(default_factory=dict)  # company -> first day its alerts are normal again
    poll_interval: int = 300
    offpeak: OffPeak | None = None
    max_pages: int = 5
    seed_max_pages: int = 50
    seed_page_delay: float = 1.0  # seeding is the one burst-shaped moment; Microsoft 429s on bursts
    max_concurrent_sources: int = 10  # ~90 sources at once caused connect timeouts through a VPN
    # First cycle only: each source starts after a random 0..N s, so a restart doesn't hit shared
    # backends (Eightfold hosts several companies) all at once.
    startup_spread: float = 60
    us_only: bool = True
    exclude_min_years: int = 4
    require_sponsorship: bool = True
    repost: RepostRules = field(default_factory=RepostRules)
    classifier: ClassifierConfig | None = None
    # A posting the classifier couldn't reach waits this long (retried every cycle) before it alerts
    # unclassified, so a short outage doesn't skip the seniority/sponsorship/scope filters.
    classify_grace_seconds: int = 900
    ntfy: NtfyConfig | None = None
    db_path: str = "jobwatch.db"
    heartbeat_url: str | None = None
    handoff_dir: str | None = None  # inbox/outbox folder shared with an external consumer
    failure_alert_after: int = 3

    def rules_for(self, company: str) -> TitleRules:
        return self.company_rules.get(company, self.title_rules)

    def is_snoozed(self, company: str, now: datetime) -> bool:
        until = self.snoozes.get(company)
        return until is not None and now.date() < until

    def interval_at(self, now: datetime) -> int:
        if self.offpeak and not self.offpeak.is_busy(now):
            return self.offpeak.interval
        return self.poll_interval


def load_config(path: str | Path) -> Config:
    raw: dict[str, Any] = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    f = raw.get("filters", {})
    base = TitleRules.from_config(f.get("include_title", []), f.get("seniority_title", []), f.get("exclude_title", []))

    sources, company_rules, source_intervals, snoozes = [], {}, {}, {}
    for spec in raw["sources"]:
        if spec.get("enabled", True) is False:
            continue
        spec = {k: v for k, v in spec.items() if k != "enabled"}
        sources.append(build_source(spec))
        if extra := spec.get("exclude_title"):
            company_rules[spec["company"]] = base.extend(extra)
        if interval := spec.get("interval_seconds"):
            source_intervals[spec["company"]] = interval
        if until := spec.get("snooze_until"):
            snoozes[spec["company"]] = until if isinstance(until, date) else date.fromisoformat(until)

    c = raw.get("classifier", {})
    api_key = os.getenv("GEMINI_API_KEY") or None
    # Its own name: ANTHROPIC_API_KEY in a shell would move the resume worker's `claude -p` to API billing.
    claude_key = os.getenv("JOBWATCH_ANTHROPIC_API_KEY") or None
    classifier = None
    if c.get("enabled", True) and (api_key or claude_key):
        classifier = ClassifierConfig(api_key=api_key, claude_api_key=claude_key,
                                      **{k: v for k, v in c.items() if k != "enabled"})

    ntfy = None
    if topic := os.getenv("NTFY_TOPIC"):
        ntfy = NtfyConfig(os.getenv("NTFY_SERVER", "https://ntfy.sh"), topic, os.getenv("NTFY_TOKEN") or None,
                          resume_topic=os.getenv("NTFY_TOPIC_RESUMES") or None,
                          low_topic=os.getenv("NTFY_TOPIC_LOW") or None)

    offpeak = None
    if (o := raw.get("offpeak")) and o.get("enabled", True):
        offpeak = OffPeak(
            interval=o.get("interval_seconds", 1800),
            timezone=o.get("timezone", "America/Los_Angeles"),
            busy_hours=tuple(o.get("busy_hours", (6, 21))),
            weekdays_only=o.get("weekdays_only", True),
        )

    return Config(
        sources=sources,
        title_rules=base,
        company_rules=company_rules,
        source_intervals=source_intervals,
        snoozes=snoozes,
        poll_interval=raw.get("poll_interval_seconds", 300),
        offpeak=offpeak,
        max_pages=raw.get("max_pages", 5),
        seed_max_pages=raw.get("seed_max_pages", 50),
        startup_spread=raw.get("startup_spread_seconds", 60),
        us_only=f.get("us_only", True),
        exclude_min_years=f.get("exclude_min_years", 4),
        require_sponsorship=f.get("require_sponsorship", True),
        repost=RepostRules(**raw.get("repost", {})),
        classifier=classifier,
        classify_grace_seconds=raw.get("classify_grace_seconds", 900),
        ntfy=ntfy,
        db_path=os.getenv("JOBWATCH_DB", "jobwatch.db"),
        heartbeat_url=os.getenv("HEARTBEAT_URL") or None,
        handoff_dir=os.getenv("HANDOFF_DIR") or None,
    )
