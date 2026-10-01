"""jobs.apple.com v1 API. No token or cookie needed today.

totalRecords == 0 never means "no jobs": the API returns it for malformed requests (unknown
keys, missing `format`) and, randomly, for ~1 in 5 valid ones. Those are retried and then
raised. The end of pagination is decided by totalRecords, never by an empty page alone.
Results are one row per location; rows are merged per positionId so a role alerts once.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import datetime
from typing import Any

import httpx

from jobwatch.models import Job
from jobwatch.sources.base import Source, SourceError, html_to_text, request_json

BASE = "https://jobs.apple.com"
_FORMAT = {"longDate": "MMMM D, YYYY", "mediumDate": "MMM D, YYYY"}
ATTEMPTS = 4
RETRY_DELAY = 1.0


class AppleSource(Source):
    def __init__(self, company: str, teams: list[str], locations: list[str] | None = None):
        super().__init__(company)
        self.filters = {
            "locations": locations or ["postLocation-USA"],
            "teams": [{"team": t} for t in teams],
        }

    async def pages(self, client: httpx.AsyncClient) -> AsyncIterator[list[Job]]:
        page, seen = 1, 0
        while True:
            total, rows = await self._search(client, page)
            if not rows:
                return
            yield self._merge(rows)
            seen += len(rows)
            if seen >= total:
                return
            page += 1

    async def _search(self, client: httpx.AsyncClient, page: int) -> tuple[int, list[dict[str, Any]]]:
        body = {"query": "", "filters": self.filters, "page": page, "locale": "en-us", "sort": "newest",
                "format": _FORMAT}
        for attempt in range(ATTEMPTS):
            if attempt:
                await asyncio.sleep(RETRY_DELAY)
            data = await request_json(client, "POST", f"{BASE}/api/v1/search", json=body)
            try:
                total, rows = data["res"]["totalRecords"], data["res"]["searchResults"] or []
            except (KeyError, TypeError) as e:
                raise SourceError(f"unexpected Apple payload: {str(data)[:200]}") from e
            if total:
                return total, rows
        raise SourceError(f"Apple returned 0 results {ATTEMPTS} times for page {page}")

    async def enrich(self, client: httpx.AsyncClient, job: Job) -> None:
        data = await request_json(client, "GET", f"{BASE}/api/v1/jobDetails/{job.source_id}",
                                  params={"locale": "en-us"})
        d = data.get("res") or {}
        sections = [
            d.get("jobSummary"), d.get("description"), d.get("responsibilities"),
            "Minimum Qualifications\n" + (d.get("minimumQualifications") or ""),
            "Preferred Qualifications\n" + (d.get("preferredQualifications") or ""),
        ]
        job.description = html_to_text("\n".join(s for s in sections if s))

    def _merge(self, rows: list[dict[str, Any]]) -> list[Job]:
        jobs: dict[str, Job] = {}
        for r in rows:
            locs = [f"{loc['name']}, {loc['countryName']}" for loc in r.get("locations") or []]
            if (pid := r["positionId"]) in jobs:
                jobs[pid].locations.extend(locs)
                continue
            jobs[pid] = Job(
                company=self.company,
                source_id=pid,
                title=" ".join(r["postingTitle"].split()),
                url=f"{BASE}/en-us/details/{r['id']}/{r['transformedPostingTitle']}",
                locations=locs,
                posted_at=datetime.fromisoformat(r["postDateInGMT"].replace("Z", "+00:00")),
            )
        return list(jobs.values())
