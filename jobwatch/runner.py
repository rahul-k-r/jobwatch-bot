from __future__ import annotations

import asyncio
import json
import logging
import random
import re
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlencode

import httpx

from jobwatch.classifier import GeminiClassifier, exclusion_reason
from jobwatch.config import Config, load_config
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
        config_path: Path | None = None,
    ):
        self.cfg, self.db, self.notifier, self.classifier, self.clock = cfg, db, notifier, classifier, clock
        self.handoff = handoff
        self.config_path = config_path
        self._config_mtime = self._mtime()
        self.failures: dict[str, int] = {}
        self.retry_at: dict[str, datetime] = {}
        self.last_poll: dict[str, datetime] = {}
        self.classify_failed_since: dict[str, datetime] = {}
        self.classifier_down = False  # one notice per outage, however many postings are waiting
        self.started = False

    def _mtime(self) -> tuple[int, ...] | None:
        """Modification times of config.yaml and the catalog it names; any change means reload."""
        if not self.config_path:
            return None

        def stamp(p: Path) -> int:
            try:
                return p.stat().st_mtime_ns
            except OSError:
                return -1

        return tuple(stamp(p) for p in dict.fromkeys([self.config_path, *self.cfg.watched]))

    def reload_if_changed(self) -> set[str]:
        """Swaps in config.yaml if it changed on disk. Returns the companies whose source was added or
        rebuilt, so the caller can poll them now. A bad edit keeps the current config. Only called between
        cycles, so nothing is mid-poll. Env-derived settings (ntfy, db, handoff) never change at runtime."""
        mtime = self._mtime()
        if mtime is None or mtime == self._config_mtime:
            return set()
        self._config_mtime = mtime  # recorded first: a broken file is reported once per save, not per check
        try:
            new = load_config(self.config_path)
        except Exception as e:
            log.error("config reload failed, keeping the current config: %r", e)
            return set()
        old = self.cfg
        keep = {s.company: s for s in old.sources if old.source_specs.get(s.company) == new.source_specs.get(s.company)}
        new.sources = [keep.get(s.company, s) for s in new.sources]
        changed = {s.company for s in new.sources if s.company not in keep}
        removed = set(old.source_specs) - set(new.source_specs)
        for state in (self.failures, self.retry_at, self.last_poll):
            for name in removed:
                state.pop(name, None)
        if self.classifier and new.classifier:
            self.classifier.cfg = new.classifier
        elif bool(self.classifier) != bool(new.classifier):
            log.warning("classifier on/off is not hot-reloadable; restart to apply")
        self.cfg = new
        self._config_mtime = self._mtime()  # the new config may watch a different set of catalog files
        log.info("config reloaded: %d sources (%d added or changed, %d removed)", len(new.sources), len(changed),
                 len(removed))
        return changed

    async def idle(self, client: httpx.AsyncClient, seconds: float, check_every: float = 2.0) -> None:
        """Waits out the gap between cycles, applying config edits as they land and polling only what they touched."""
        end = time.monotonic() + seconds
        while (left := end - time.monotonic()) > 0:
            await asyncio.sleep(min(left, check_every))
            if changed := self.reload_if_changed():
                await self.cycle(client, only=changed)

    async def cycle(self, client: httpx.AsyncClient, only: set[str] | None = None) -> None:
        limit = asyncio.Semaphore(self.cfg.max_concurrent_sources)
        spread = 0 if self.started else self.cfg.startup_spread
        self.started = True

        async def poll(source: Source) -> None:
            if spread:
                await asyncio.sleep(random.uniform(0, spread))
            async with limit:
                await self._poll_guarded(client, source)

        await asyncio.gather(*(poll(s) for s in self.cfg.sources if only is None or s.company in only))
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
        self.db.mark_polled(name, now)
        self.retry_at.pop(name, None)
        if self.failures.pop(name, 0) >= self.cfg.failure_alert_after:
            await self._notice(f"{name} source recovered", "Polling works again.")

    async def poll_source(self, client: httpx.AsyncClient, source: Source) -> None:
        seeding = not self.db.is_seeded(source.company)
        max_pages = self.cfg.seed_max_pages if seeding else self.cfg.max_pages
        prev_poll = self.db.last_polled(source.company)
        pages = 0
        async for page in source.pages(client):
            pages += 1
            known = self.db.known_keys(j.key for j in page)
            fresh = [j for j in page if j.key not in known]
            if seeding:
                self.db.insert_many(fresh, "seeded", self.clock())
            else:
                for job in fresh:
                    job.prev_poll_at = prev_poll
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
        waiting = await self._classify(todo) if self.classifier and todo else set()

        alerts = []
        for job in jobs:
            if job.key in waiting:
                continue  # stays pending; retried next cycle
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

    async def _classify(self, todo: list[Job]) -> set[str]:
        """Classifies `todo` in place; returns the keys that should keep waiting for a later cycle."""
        results = await self.classifier.classify(todo)
        now = self.clock()
        waiting: set[str] = set()
        for j in todo:
            if j.key in results:
                j.classification = results[j.key]
                self.classify_failed_since.pop(j.key, None)
                continue
            since = self.classify_failed_since.setdefault(j.key, now)
            if (now - since).total_seconds() < self.cfg.classify_grace_seconds:
                waiting.add(j.key)
            else:
                # Fail open so an outage can't cost a posting. Stored so a failed alert retry
                # doesn't spend another LLM call.
                j.classification = {"unclassified": True}
                self.classify_failed_since.pop(j.key, None)
        if len(waiting) == 0 and all(j.key in results for j in todo):
            self.classifier_down = False
        elif waiting and not self.classifier_down:
            self.classifier_down = True
            n = len(waiting)
            await self._notice(
                "Classifier unreachable",
                f"{n} posting{'s' if n != 1 else ''} held for another try. "
                f"They alert unclassified after {self.cfg.classify_grace_seconds // 60} min. Check the network or VPN.",
            )
        return waiting

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
            attachment = None
            copy = None
            if r.get("status") == "ready":
                warnings = "".join(f"\n⚠ {w}" for w in r.get("warnings") or [])
                attachment = _read_pdf(r.get("pdf"))
                note = "" if attachment else "\n(PDF not attached: file missing or not a PDF)"
                title, message, priority = f"Resume ready: {name}", f"{_job_pdf(r.get('pdf'))}{warnings}{note}", Priority.HIGH
                answers = [a for a in r.get("answers") or []
                           if isinstance(a, dict) and isinstance(a.get("question"), str) and isinstance(a.get("answer"), str)]
                if answers:
                    text, dropped = answers_copy(answers)
                    n = len(answers) - dropped
                    message += f"\n✍ {n} application answer{'s' if n != 1 else ''} drafted. Review before pasting."
                    if dropped:
                        message += f" {dropped} more didn't fit the button; see answers.json beside the CV."
                    copy = ("Copy answers", text) if n else None
            else:
                title, message, priority = f"Resume not built: {name}", _strip_paths(str(r.get("reason"))), Priority.DEFAULT
            try:
                await self.notifier.send_text(title, message, priority, click=r.get("url"), attachment=attachment,
                                              channel="resumes", copy=copy)
            except Exception as e:
                log.error("could not send %r, will retry: %r", title, e)
                continue
            self.handoff.mark_sent(path)

    def _alert(self, job: Job) -> Alert:
        c = job.classification or {}
        labels = []
        snoozed = self.cfg.is_snoozed(job.company, self.clock())
        if snoozed:
            labels.append("snoozed")
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
        return Alert(job, Priority.LOW if snoozed or job.repost_reason else Priority.HIGH, labels)

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


MAX_ATTACHMENT_BYTES = 15 * 1024 * 1024  # ntfy.sh's per-file limit


def _read_pdf(pdf: str | None) -> tuple[str, bytes] | None:
    """(<job folder>.pdf, bytes) for upload, or None. The path comes from another process's output,
    so anything that isn't a PDF is refused rather than sent to a third-party server."""
    if not pdf:
        return None
    path = Path(pdf)
    try:
        if path.suffix.lower() != ".pdf" or path.stat().st_size > MAX_ATTACHMENT_BYTES:
            return None
        data = path.read_bytes()
    except OSError:
        return None
    if not data.startswith(b"%PDF-"):
        return None
    # Every build is cv.pdf; name the download after its job folder so they're distinguishable on the phone.
    return f"{path.parent.name or 'resume'}.pdf", data


# With a PDF the button travels in the upload's URL; ntfy.sh answered 414 at ~12KB, 6KB went through.
MAX_COPY_ENCODED = 6000


def answers_copy(answers: list[dict[str, str]]) -> tuple[str, int]:
    """All Q/A pairs as one clipboard text, plus how many trailing pairs were left out to fit."""
    pairs = [f"Q: {a['question'].strip()}\nA: {a['answer'].strip()}" for a in answers]
    for n in range(len(pairs), 0, -1):
        text = "\n\n".join(pairs[:n])
        action = json.dumps([{"action": "copy", "label": "Copy answers", "value": text}])
        if len(urlencode({"actions": action})) <= MAX_COPY_ENCODED:
            return text, len(pairs) - n
    return "", len(pairs)


def _strip_paths(text: str) -> str:
    return _ABS_PATH.sub(lambda m: re.split(r"[\\/]", m.group(0))[-1], text)


async def run(cfg: Config, once: bool = False, config_path: Path | None = None) -> None:
    db = DB(cfg.db_path)
    async with httpx.AsyncClient(timeout=30, headers={"User-Agent": USER_AGENT}, follow_redirects=True) as client:
        notifier: Notifier = (
            NtfyNotifier(client, cfg.ntfy.server, cfg.ntfy.topic, cfg.ntfy.token,
                         resume_topic=cfg.ntfy.resume_topic, low_topic=cfg.ntfy.low_topic)
            if cfg.ntfy else ConsoleNotifier()
        )
        classifier = GeminiClassifier(cfg.classifier, db, client) if cfg.classifier else None
        handoff = Handoff(cfg.handoff_dir) if cfg.handoff_dir else None
        log.info("watching %d sources; notifier=%s; classifier=%s; resume handoff=%s", len(cfg.sources),
                 type(notifier).__name__, cfg.classifier.model if cfg.classifier else "off", cfg.handoff_dir or "off")
        watcher = Watcher(cfg, db, notifier, classifier, handoff=handoff, config_path=None if once else config_path)
        # "Resume ready" should arrive when the CV is built, not at the next poll minutes later.
        results = asyncio.create_task(_deliver_results_forever(watcher)) if handoff and not once else None
        try:
            while True:
                started = time.monotonic()
                await watcher.cycle(client)
                if cfg.heartbeat_url:  # env-derived, so the original cfg is as good as the reloaded one
                    try:
                        await client.get(cfg.heartbeat_url)
                    except httpx.HTTPError as e:
                        log.warning("heartbeat failed: %r", e)
                if once:
                    await watcher.deliver_handoff_results()
                    return
                # Jitter so polls don't land on exact, bot-like intervals.
                interval = watcher.cfg.interval_at(datetime.now(UTC))
                delay = interval - (time.monotonic() - started) + random.uniform(-0.1, 0.1) * interval
                await watcher.idle(client, max(delay, 5))
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
