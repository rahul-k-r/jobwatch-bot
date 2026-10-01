"""Gemini batch classifier: seniority / required years / repost mention / sponsorship.

Fails open: any error (quota, 429, bad JSON) returns None and the caller alerts unclassified.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from jobwatch.db import DB
from jobwatch.models import Job

log = logging.getLogger(__name__)

_API = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
_PACIFIC = ZoneInfo("America/Los_Angeles")  # Gemini daily quotas reset at midnight Pacific
_TRANSIENT = {429, 500, 502, 503, 504}
RETRY_DELAYS = (5, 15)  # seconds before each retry of a transient failure

_SCHEMA = {
    "type": "ARRAY",
    "items": {
        "type": "OBJECT",
        "properties": {
            "id": {"type": "STRING"},
            "level": {"type": "STRING", "enum": ["entry", "mid", "senior", "staff_plus", "unknown"]},
            "min_years": {"type": "INTEGER", "nullable": True},
            "mentions_repost": {"type": "BOOLEAN"},
            "sponsorship": {"type": "STRING", "enum": ["available", "not_available", "unspecified"]},
            "phd_required": {"type": "BOOLEAN"},
            "in_scope": {"type": "BOOLEAN"},
            "reason": {"type": "STRING"},
        },
        "required": ["id", "level", "min_years", "mentions_repost", "sponsorship", "phd_required", "in_scope",
                     "reason"],
    },
}

_PROMPT = """You classify job postings for a software engineer with a Master's degree and about 3 years of experience who needs US visa sponsorship.
For each posting return:
- level: entry | mid | senior | staff_plus | unknown, judged from the title AND the requirements (internal level codes such as IC3 / L4 / SDE II count).
- min_years: the minimum years of experience the REQUIRED/basic qualifications ask for (ignore preferred qualifications). Where several degree paths are listed (e.g. "Bachelor's + 4 years OR Master's + 2 years"), use the Master's-degree path. null if not stated.
- mentions_repost: true only if the text says this is a repost / re-posting or addresses people who previously applied.
- sponsorship: not_available only if the text says sponsorship is not offered, US citizenship is required, or a security clearance is needed. Generic equal-opportunity language mentioning citizenship or immigration status is NOT a restriction.
- phd_required: true only if the REQUIRED qualifications demand a PhD and do not accept a Master's degree or equivalent experience instead. "PhD preferred", "PhD or MS", or a PhD listed only under preferred qualifications is false.
- in_scope: true only if the job itself is hands-on technical work: software / ML / data / infrastructure engineering, forward deployed / solutions / customer / integration engineering, or research engineering / science. false for recruiting or sourcing, sales or account roles, program / product / project management, design, operations, support, data labeling or tutoring, legal, finance and marketing, even if the title mentions engineers or engineering.
- reason: under 15 words.
Return one object per posting, echoing its id.

"""


@dataclass
class ClassifierConfig:
    api_key: str
    model: str = "gemini-flash-lite-latest"
    daily_limit: int = 450
    requests_per_minute: int = 10
    # Separate free-tier quota; slower (~25s per call) so it's only used when the primary fails.
    fallback_model: str | None = None
    fallback_daily_limit: int = 10000
    fallback_requests_per_minute: int = 10
    batch_size: int = 10
    max_chars: int = 5000
    timeout: float = 120  # Gemma needs far longer than the default 30s client timeout

    def chain(self) -> list[tuple[str, int, int]]:
        models = [(self.model, self.daily_limit, self.requests_per_minute)]
        if self.fallback_model:
            models.append((self.fallback_model, self.fallback_daily_limit, self.fallback_requests_per_minute))
        return models


class GeminiClassifier:
    def __init__(self, cfg: ClassifierConfig, db: DB, client: httpx.AsyncClient):
        self.cfg, self.db, self.client = cfg, db, client
        self._last_call: dict[str, float] = {}

    async def classify(self, jobs: list[Job]) -> dict[str, dict[str, Any]]:
        """Returns {job.key: classification} for the jobs it managed to classify."""
        out: dict[str, dict[str, Any]] = {}
        for i in range(0, len(jobs), self.cfg.batch_size):
            batch = jobs[i : i + self.cfg.batch_size]
            result = await self._classify_batch(batch)
            if result is None:
                break  # quota or API trouble: don't burn more calls this cycle
            out.update(result)
        return out

    async def _classify_batch(self, batch: list[Job]) -> dict[str, dict[str, Any]] | None:
        ids = {str(n): job for n, job in enumerate(batch)}
        prompt = _PROMPT + "\n\n".join(
            f"=== id: {n}\nTitle: {job.title}\nText:\n{_excerpt(job.description or '', self.cfg.max_chars)}"
            for n, job in ids.items()
        )
        body = {
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {"responseMimeType": "application/json", "responseSchema": _SCHEMA, "temperature": 0},
        }
        for model, daily_limit, rpm in self.cfg.chain():
            items = await self._call(model, daily_limit, rpm, body)
            if items is not None:
                return {ids[str(it["id"])].key: it for it in items if str(it.get("id")) in ids}
        return None

    async def _call(self, model: str, daily_limit: int, rpm: int, body: dict[str, Any]) -> list[dict] | None:
        for attempt, delay in enumerate((0, *RETRY_DELAYS)):
            if delay:
                await asyncio.sleep(delay)
            if not await self._take_budget(model, daily_limit, rpm):
                return None
            try:
                resp = await self.client.post(_API.format(model=model), json=body, timeout=self.cfg.timeout,
                                              headers={"x-goog-api-key": self.cfg.api_key})
                resp.raise_for_status()
                return json.loads(resp.json()["candidates"][0]["content"]["parts"][0]["text"])
            except httpx.HTTPStatusError as e:
                if e.response.status_code in _TRANSIENT and attempt < len(RETRY_DELAYS):
                    continue
                log.warning("%s classification failed: %r", model, e)
                return None
            except httpx.TransportError as e:
                if attempt < len(RETRY_DELAYS):
                    continue
                log.warning("%s classification failed: %r", model, e)
                return None
            except (KeyError, IndexError, ValueError) as e:
                log.warning("%s returned an unusable response: %r", model, e)
                return None
        return None

    async def _take_budget(self, model: str, daily_limit: int, rpm: int) -> bool:
        """Every HTTP attempt counts against that model's free-tier quota, retries included."""
        key = f"{datetime.now(_PACIFIC).date().isoformat()}|{model}"
        if self.db.llm_requests(key) >= daily_limit:
            log.warning("%s daily budget (%d) used up", model, daily_limit)
            return False
        wait = self._last_call.get(model, 0.0) + 60 / rpm - time.monotonic()
        if wait > 0:
            await asyncio.sleep(wait)
        self._last_call[model] = time.monotonic()
        self.db.add_llm_request(key)
        return True


_QUALS = re.compile(r"(required|basic|minimum)\s+qualifications|qualifications|requirements", re.I)


def _excerpt(text: str, limit: int) -> str:
    """Requirements usually sit mid-to-late in a posting; make sure they survive truncation."""
    if len(text) <= limit:
        return text
    m = _QUALS.search(text)
    if not m:
        return text[:limit]
    head = text[: limit // 4]
    return head + "\n...\n" + text[m.start() : m.start() + limit - len(head)]


def exclusion_reason(c: dict[str, Any], exclude_min_years: int, require_sponsorship: bool) -> str | None:
    # `is False`: classifications stored before this field existed fail open.
    if c.get("in_scope") is False:
        return f"not an engineering role (llm: {c.get('reason')})"
    if require_sponsorship and c.get("sponsorship") == "not_available":
        return f"no sponsorship (llm: {c.get('reason')})"
    if c.get("phd_required"):
        return f"PhD required (llm: {c.get('reason')})"
    years = c.get("min_years")
    # Required years is the objective signal; the level label only decides when years aren't stated.
    if years is not None:
        if years >= exclude_min_years:
            return f"requires {years}+ years (llm)"
    elif c.get("level") in ("senior", "staff_plus"):
        return f"{c.get('level')} (llm: {c.get('reason')})"
    return None
