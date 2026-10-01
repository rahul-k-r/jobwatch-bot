from __future__ import annotations

import asyncio
import logging
import random
import re
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import httpx

from jobwatch.classifier import GeminiClassifier, exclusion_reason
from jobwatch.config import Config
from jobwatch.db import DB
from jobwatch.filters import is_us, no_sponsorship_reason
from jobwatch.handoff import Handoff
from jobwatch.injection import scan
from jobwatch.models import Alert, Job, Priority
from jobwatch.notify import ConsoleNotifier, Notifier, NtfyNotifier
from jobwatch.repost import repost_reason
from jobwatch.sources.base import Source, SourceError

log = logging.getLogger(__name__)

# Honest client name. A Chrome UA without Chrome's other headers is a classic bot
# fingerprint (Meta rejects exactly that); several of these APIs accept a plain client.
USER_AGENT = "jobwatch/0.1 (personal job alerts)"
MAX_BACKOFF_SECONDS = 1800


class Watcher:
    def __init__(
        self,
        cfg: Config,
        db: DB,
        notifier: Notifier,
        classifier: GeminiClassifier | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        handoff: Handoff | None = None,
    ):
        self.cfg, self.db, self.notifier, self.classifier, self.clock = cfg, db, notifier, classifier, clock
        self.handoff = handoff
        self.failures: dict[str, int] = {}
        self.retry_at: dict[str, datetime] = {}
        self.last_poll: dict[str, datetime] = {}

    async def cycle(self, client: httpx.AsyncClient) -> None:
        limit = asyncio.Semaphore(self.cfg.max_concurrent_sources)

        async def poll(source: Source) -> None:
            async with limit:
                await self._poll_guarded(client, source)

        await asyncio.gather(*(poll(s) for s in self.cfg.sources))
        await self.process_pending()

    async def _poll_guarded(self, client: httpx.AsyncClient, source: Source) -> None:
        name = source.company
        now = self.clock()
        if name in self.retry_at and now < self.retry_at[name]:
            return
        interval = self.cfg.source_intervals.get(name)
        if interval and name in self.last_poll and (now - self.last_poll[name]).total_seconds() < interval:
            return
        self.last_poll[name] = now
        try:
            await self.poll_source(client, source)
        except Exception as e:  # one broken source must not stop the others
            n = self.failures[name] = self.failures.get(name, 0) + 1
            # Exponential backoff so a rate-limited source isn't hammered every cycle.
            delay = min(self.cfg.poll_interval * 2 ** (n - 1), MAX_BACKOFF_SECONDS)
            if isinstance(e, SourceError) and e.retry_after:
                delay = max(delay, e.retry_after)
            self.retry_at[name] = self.clock() + timedelta(seconds=delay)
            log.warning("%s: poll failed (%d in a row), retrying in %ds: %r", name, n, delay, e)
            if n == self.cfg.failure_alert_after:
                await self._notice(f"{name} source failing", f"{n} failed polls in a row. Last error: {e!r}"[:500])
            return
        self.retry_at.pop(name, None)
        if self.failures.pop(name, 0) >= self.cfg.failure_alert_after:
            await self._notice(f"{name} source recovered", "Polling works again.")

    async def poll_source(self, client: httpx.AsyncClient, source: Source) -> None:
        seeding = not self.db.is_seeded(source.company)
        max_pages = self.cfg.seed_max_pages if seeding else self.cfg.max_pages
        pages = 0
        async for page in source.pages(client):
            pages += 1
            known = self.db.known_keys(j.key for j in page)
            fresh = [j for j in page if j.key not in known]
            if seeding:
                self.db.insert_many(fresh, "seeded", self.clock())
            else:
                for job in fresh:
                    await self._admit(client, source, job)
            if (not seeding and not fresh) or pages >= max_pages:
                break
            if seeding:
                await asyncio.sleep(self.cfg.seed_page_delay)
        if seeding:
            self.db.mark_seeded(source.company, self.clock())
            log.info("%s: seeded", source.company)

    async def _admit(self, client: httpx.AsyncClient, source: Source, job: Job) -> None:
        now = self.clock()
        reason = self.cfg.rules_for(job.company).reject_reason(job.title)
        if not reason and self.cfg.us_only and not is_us(job):
            reason = "outside US"
        if reason:
            self.db.insert(job, "filtered", now, reason)
            return
        try:
            await source.enrich(client, job)
        except SourceError as e:
            # Alert without a description rather than risk missing the job.
            log.warning("%s: detail fetch failed for %s: %r", job.company, job.source_id, e)
        found = scan(job.description)
        job.ai_instructions, job.ai_policy, job.canaries = found.instructions, found.policy, found.canaries
        # Some sources (Workday) only know locations after the detail call.
        if self.cfg.us_only and not is_us(job):
            reason = "outside US"
        elif self.cfg.require_sponsorship:
            reason = no_sponsorship_reason(job)
        if reason:
            self.db.insert(job, "filtered", now, reason)
            return
        job.repost_reason = repost_reason(job, self.db, now, self.cfg.repost)
        self.db.insert(job, "pending", now)
        log.info("%s: new posting %r%s", job.company, job.title, " (repost)" if job.repost_reason else "")

    async def process_pending(self) -> None:
        jobs = self.db.pending()
        if not jobs:
            return
        todo = [j for j in jobs if j.classification is None]
        if self.classifier and todo:
            results = await self.classifier.classify(todo)
            for j in todo:
                # Stored even when unclassified so a failed alert retry doesn't spend another LLM call.
                j.classification = results.get(j.key, {"unclassified": True})

        alerts = []
        for job in jobs:
            c = job.classification or {}
            if c and not c.get("unclassified"):
                if reason := exclusion_reason(c, self.cfg.exclude_min_years, self.cfg.require_sponsorship):
                    self.db.update(job, "filtered", reason)
                    continue
                if c.get("mentions_repost") and not job.repost_reason:
                    job.repost_reason = "posting mentions a repost (llm)"
            alerts.append(self._alert(job))
        if not alerts:
            return
        try:
            await self.notifier.send(alerts)
        except Exception as e:
            # Delivery is at-least-once: a partial failure re-sends the batch next cycle.
            log.error("alert delivery failed, will retry: %r", e)
            for a in alerts:
                self.db.update(a.job, "pending")
            return
        now = self.clock()
        for a in alerts:
            self.db.update(a.job, "notified", a.job.repost_reason, notified_at=now)
            if self.handoff and self._wants_resume(a):
                try:
                    self.handoff.queue(a.job)
                except OSError as e:
                    log.error("could not hand %s off for a resume: %r", a.job.key, e)

    @staticmethod
    def _wants_resume(alert: Alert) -> bool:
        # A CV build takes minutes of Claude time; only spend it on jobs the classifier has cleared.
        c = alert.job.classification or {}
        return alert.priority == Priority.HIGH and bool(c) and not c.get("unclassified")

    async def deliver_handoff_results(self) -> None:
        if not self.handoff:
            return
        for path, r in self.handoff.collect():
            name = f"{r.get('company')} - {r.get('title')}"
            if r.get("status") == "ready":
                warnings = "".join(f"\n⚠ {w}" for w in r.get("warnings") or [])
                title, message, priority = f"Resume ready: {name}", f"{_job_pdf(r.get('pdf'))}{warnings}", Priority.HIGH
            else:
                title, message, priority = f"Resume not built: {name}", _strip_paths(str(r.get("reason"))), Priority.DEFAULT
            try:
                await self.notifier.send_text(title, message, priority, click=r.get("url"))
            except Exception as e:
                log.error("could not send %r, will retry: %r", title, e)
                continue
            self.handoff.mark_sent(path)

    def _alert(self, job: Job) -> Alert:
        c = job.classification or {}
        labels = []
        if c.get("min_years") is not None:
            labels.append(f"{c['min_years']}+ yrs")
        if c.get("unclassified"):
            labels.append("unclassified")
        if job.repost_reason:
            labels.append(f"repost: {job.repost_reason}")
        if job.ai_instructions:
            labels.append("⚠ AI-directed instructions")
        if job.ai_policy:
            labels.append("⚠ rejects AI-written applications")
        return Alert(job, Priority.LOW if job.repost_reason else Priority.HIGH, labels)

    async def _notice(self, title: str, message: str) -> None:
        try:
            await self.notifier.send_text(title, message, Priority.DEFAULT)
        except Exception as e:
            log.error("could not send notice %r: %r", title, e)


# Pushes go through a third-party server; local paths (username, folder layout) stay out of them.
_ABS_PATH = re.compile(r"[A-Za-z]:[\\/]\S*|/(?:home|Users|root|tmp)/\S*")


def _job_pdf(pdf: str | None) -> str:
    """'.../output/jobwatch/<job folder>/cv.pdf' -> '<job folder>/cv.pdf'."""
    parts = re.split(r"[\\/]", pdf or "")
    return "/".join(parts[-2:]) if pdf else "PDF path missing"


def _strip_paths(text: str) -> str:
    return _ABS_PATH.sub(lambda m: re.split(r"[\\/]", m.group(0))[-1], text)


async def run(cfg: Config, once: bool = False) -> None:
    db = DB(cfg.db_path)
    async with httpx.AsyncClient(timeout=30, headers={"User-Agent": USER_AGENT}, follow_redirects=True) as client:
        notifier: Notifier = (
            NtfyNotifier(client, cfg.ntfy.server, cfg.ntfy.topic, cfg.ntfy.token) if cfg.ntfy else ConsoleNotifier()
        )
        classifier = GeminiClassifier(cfg.classifier, db, client) if cfg.classifier else None
        handoff = Handoff(cfg.handoff_dir) if cfg.handoff_dir else None
        log.info("watching %d sources; notifier=%s; classifier=%s; resume handoff=%s", len(cfg.sources),
                 type(notifier).__name__, cfg.classifier.model if cfg.classifier else "off", cfg.handoff_dir or "off")
        watcher = Watcher(cfg, db, notifier, classifier, handoff=handoff)
        # "Resume ready" should arrive when the CV is built, not at the next poll minutes later.
        results = asyncio.create_task(_deliver_results_forever(watcher)) if handoff and not once else None
        try:
            while True:
                started = time.monotonic()
                await watcher.cycle(client)
                if cfg.heartbeat_url:
                    try:
                        await client.get(cfg.heartbeat_url)
                    except httpx.HTTPError as e:
                        log.warning("heartbeat failed: %r", e)
                if once:
                    await watcher.deliver_handoff_results()
                    return
                # Jitter so polls don't land on exact, bot-like intervals.
                interval = cfg.interval_at(datetime.now(UTC))
                delay = interval - (time.monotonic() - started) + random.uniform(-0.1, 0.1) * interval
                await asyncio.sleep(max(delay, 5))
        finally:
            if results:
                results.cancel()
            db.close()


async def _deliver_results_forever(watcher: Watcher, every: float = 15) -> None:
    while True:
        try:
            await watcher.deliver_handoff_results()
        except Exception as e:  # never let this loop die silently
            log.error("handoff result delivery failed: %r", e)
        await asyncio.sleep(every)
