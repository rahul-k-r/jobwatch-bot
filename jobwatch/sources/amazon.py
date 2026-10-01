"""amazon.jobs search.json. Full description is in the list response; no detail call.

sort=recent orders by an internal created date, not posted_date, so the order isn't
strictly by the dates we see. Pages of 100 (the max) make that irrelevant in practice.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

import httpx

from jobwatch.models import Job
from jobwatch.sources.base import Source, SourceError, html_to_text, request_json

BASE = "https://www.amazon.jobs"
PAGE_SIZE = 100


class AmazonSource(Source):
    def __init__(self, company: str, params: dict[str, Any] | None = None):
        super().__init__(company)
        self.params = params or {}

    async def pages(self, client: httpx.AsyncClient) -> AsyncIterator[list[Job]]:
        offset = 0
        while True:
            params = {**self.params, "sort": "recent", "result_limit": PAGE_SIZE, "offset": offset}
            body = await request_json(client, "GET", f"{BASE}/en/search.json", params=params)
            if not isinstance(body, dict) or body.get("error"):
                raise SourceError(f"Amazon search error: {str(body)[:200]}")
            jobs = body.get("jobs") or []
            if not jobs:
                if offset < body.get("hits", 0):
                    raise SourceError(f"empty page at offset={offset} of {body.get('hits')}")
                return
            yield [self._to_job(j) for j in jobs]
            offset += len(jobs)
            if offset >= body.get("hits", 0):
                return

    def _to_job(self, j: dict[str, Any]) -> Job:
        sections = [
            j.get("description") or "",
            "Basic Qualifications\n" + (j.get("basic_qualifications") or ""),
            "Preferred Qualifications\n" + (j.get("preferred_qualifications") or ""),
        ]
        return Job(
            company=self.company,
            source_id=str(j["id_icims"]),
            title=j["title"].strip(),
            url=BASE + j["job_path"],
            locations=[j["location"]] if j.get("location") else [],
            posted_at=_date(j.get("posted_date")),
            description=html_to_text("<br/>".join(sections)),
        )


def _date(s: str | None) -> datetime | None:
    try:
        return datetime.strptime(s, "%B %d, %Y").replace(tzinfo=UTC) if s else None
    except ValueError:
        return None
