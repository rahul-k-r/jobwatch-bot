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
_CLAUDE_API = "https://api.anthropic.com/v1/messages"
_PACIFIC = ZoneInfo("America/Los_Angeles")  # Gemini daily quotas reset at midnight Pacific
_TRANSIENT = {429, 500, 502, 503, 504}
RETRY_DELAYS = (5, 15)  # seconds before each retry of a transient failure

_SCHEMA = {
    "type": "ARRAY",
    "items": {
        "type": "OBJECT",
        "properties": {
            "id": {"type": "STRING"},
            "level": {"type": "STRING", "enum": ["intern", "entry", "mid", "senior", "staff_plus", "unknown"]},
            "min_years": {"type": "INTEGER", "nullable": True},
            "mentions_repost": {"type": "BOOLEAN"},
            "sponsorship": {"type": "STRING", "enum": ["available", "not_available", "unspecified"]},
            "phd_required": {"type": "BOOLEAN"},
            "in_scope": {"type": "BOOLEAN"},
            "highlights": {"type": "STRING"},
            "reason": {"type": "STRING"},
        },
        "required": ["id", "level", "min_years", "mentions_repost", "sponsorship", "phd_required", "in_scope",
                     "highlights", "reason"],
    },
}

_PROMPT = """You classify job postings for a software engineer with a Master's degree and about 3 years of experience who needs US visa sponsorship.
For each posting return:
- level: intern | entry | mid | senior | staff_plus | unknown, judged from the title AND the requirements (internal level codes such as IC3 / L4 / SDE II count). intern means an internship, co-op or other fixed-term student role, even if the title misspells it; a full-time new grad / university graduate role is entry, not intern.
- min_years: the minimum years of experience the REQUIRED/basic qualifications ask for (ignore preferred qualifications). Where several degree paths are listed (e.g. "Bachelor's + 4 years OR Master's + 2 years"), use the Master's-degree path. null if not stated.
- mentions_repost: true only if the text says this is a repost / re-posting or addresses people who previously applied.
- sponsorship: not_available only if the text says sponsorship is not offered, US citizenship is required, or a security clearance is needed. Generic equal-opportunity language mentioning citizenship or immigration status is NOT a restriction.
- phd_required: true only if the REQUIRED qualifications demand a PhD and do not accept a Master's degree or equivalent experience instead. "PhD preferred", "PhD or MS", or a PhD listed only under preferred qualifications is false.
- in_scope: true only if the job itself is hands-on technical work: software / ML / data / infrastructure engineering, forward deployed / solutions / customer / integration engineering, or research engineering / science. false for recruiting or sourcing, sales or account roles, program / product / project management, design, operations, support, data labeling or tutoring, legal, finance and marketing, even if the title mentions engineers or engineering.
- highlights: one line, at most 18 words, shown in a phone notification: the core stack or skills the REQUIRED qualifications ask for, what the team builds, and anything that changes whether to apply (onsite or hybrid days, travel, on-call, relocation, a degree field). Plain facts from the text, no marketing, no company name, no repeating the title or years.
- reason: under 15 words.
Return one object per posting, echoing its id.

"""


@dataclass
class ClassifierConfig:
    api_key: str | None  # Gemini; None leaves only Claude
    model: str = "gemini-flash-lite-latest"
    daily_limit: int = 450
    requests_per_minute: int = 10
    # Separate free-tier quota; slower (~25s per call) so it's only used when the primary fails.
    fallback_model: str | None = None
    fallback_daily_limit: int = 10000
    fallback_requests_per_minute: int = 10
    batch_size: int = 10
    max_parallel_batches: int = 3
    max_chars: int = 5000
    timeout: float = 120  # Gemma needs far longer than the default 30s client timeout
    # Paid, so last: only reached when both free Gemini models fail. ~10 postings per call.
    claude_api_key: str | None = None
    # Matched Sonnet 5.5 on every filtering field over 30 real postings, at ~1/15 the cost (2026-10-13).
    claude_model: str = "claude-haiku-5-5"
    claude_daily_limit: int = 100
    claude_requests_per_minute: int = 10

    def chain(self) -> list[tuple[str, int, int]]:
        models = []
        if self.api_key:
            models.append((self.model, self.daily_limit, self.requests_per_minute))
            if self.fallback_model:
                models.append((self.fallback_model, self.fallback_daily_limit, self.fallback_requests_per_minute))
        if self.claude_api_key:
            models.append((self.claude_model, self.claude_daily_limit, self.claude_requests_per_minute))
        return models


class GeminiClassifier:
    def __init__(self, cfg: ClassifierConfig, db: DB, client: httpx.AsyncClient):
        self.cfg, self.db, self.client = cfg, db, client
        self._last_call: dict[str, float] = {}
        self._budget_locks: dict[str, asyncio.Lock] = {}

    async def classify(self, jobs: list[Job]) -> dict[str, dict[str, Any]]:
        """Returns {job.key: classification} for the jobs it managed to classify. A burst's batches run
        side by side; their starts are still spaced by each model's rate limit."""
        slots = asyncio.Semaphore(self.cfg.max_parallel_batches)
        failed = False

        async def run(batch: list[Job]) -> dict[str, dict[str, Any]]:
            nonlocal failed
            async with slots:
                if failed:
                    return {}  # quota or API trouble: don't burn more calls this cycle
                result = await self._classify_batch(batch)
                failed = failed or result is None
                return result or {}

        size = self.cfg.batch_size
        results = await asyncio.gather(*(run(jobs[i : i + size]) for i in range(0, len(jobs), size)))
        return {k: v for r in results for k, v in r.items()}

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
                if model == self.cfg.claude_model:
                    return await self._call_claude(model, body)
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

    async def _call_claude(self, model: str, body: dict[str, Any]) -> list[dict]:
        """Same prompt as Gemini, with the same schema as structured output. (Not a forced tool call:
        Sonnet 5.5 rejects tool_choice "tool".)"""
        schema = {"type": "object", "properties": {"postings": _json_schema(_SCHEMA)}, "required": ["postings"],
                  "additionalProperties": False}
        resp = await self.client.post(_CLAUDE_API, timeout=self.cfg.timeout, headers={
            "x-api-key": self.cfg.claude_api_key, "anthropic-version": "2023-06-01"}, json={
            "model": model,
            "max_tokens": 8192,
            "messages": [{"role": "user", "content": body["contents"][0]["parts"][0]["text"]}],
            "output_config": {"format": {"type": "json_schema", "schema": schema}},
        })
        resp.raise_for_status()
        text = "".join(c.get("text", "") for c in resp.json()["content"] if c.get("type") == "text")
        reply = json.loads(text) if text else None
        items = reply.get("postings") if isinstance(reply, dict) else None
        if not isinstance(items, list):
            raise ValueError("no postings list in the reply")
        return items

    async def _take_budget(self, model: str, daily_limit: int, rpm: int) -> bool:
        """Every HTTP attempt counts against that model's free-tier quota, retries included."""
        # Held across the wait, so parallel batches take turns: each sees the previous start and the count.
        async with self._budget_locks.setdefault(model, asyncio.Lock()):
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


def _json_schema(s: dict[str, Any]) -> dict[str, Any]:
    """Gemini's OpenAPI-style schema (upper-case types, `nullable`) as JSON Schema for Claude."""
    out: dict[str, Any] = {"type": s["type"].lower()}
    if s.get("nullable"):
        out["type"] = [out["type"], "null"]
    for k in ("enum", "required"):
        if k in s:
            out[k] = s[k]
    if "items" in s:
        out["items"] = _json_schema(s["items"])
    if "properties" in s:
        out["properties"] = {k: _json_schema(v) for k, v in s["properties"].items()}
        out["additionalProperties"] = False  # required by structured output
    return out


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
    if c.get("level") == "intern":
        return f"internship (llm: {c.get('reason')})"
    years = c.get("min_years")
    # Required years is the objective signal; the level label only decides when years aren't stated.
    if years is not None:
        if years >= exclude_min_years:
            return f"requires {years}+ years (llm)"
    elif c.get("level") in ("senior", "staff_plus"):
        return f"{c.get('level')} (llm: {c.get('reason')})"
    return None
