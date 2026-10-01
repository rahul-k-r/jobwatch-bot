"""Greenhouse Job Board API (official, public).

The list is polled without `content=true` (35KB vs ~1MB for a large board); descriptions and
structured office locations come from the per-job detail call for new postings only.
"""

from __future__ import annotations

import html
from collections.abc import AsyncIterator
from datetime import datetime
from typing import Any

import httpx

from jobwatch.models import Job
from jobwatch.sources.base import Source, SourceError, html_to_text, request_json


class GreenhouseSource(Source):
    def __init__(self, company: str, board: str):
        super().__init__(company)
        self.api = f"https://boards-api.greenhouse.io/v1/boards/{board}"

    async def pages(self, client: httpx.AsyncClient) -> AsyncIterator[list[Job]]:
        data = await request_json(client, "GET", f"{self.api}/jobs")
        if not isinstance(data, dict) or not isinstance(data.get("jobs"), list):
            raise SourceError(f"unexpected Greenhouse payload: {str(data)[:200]}")
        if data["jobs"]:
            yield [self._to_job(j) for j in data["jobs"]]

    async def enrich(self, client: httpx.AsyncClient, job: Job) -> None:
        d = await request_json(client, "GET", f"{self.api}/jobs/{job.source_id}")
        # `content` is HTML that is itself entity-escaped.
        job.description = html_to_text(html.unescape(d.get("content") or ""))
        offices = [o.get("location") or o.get("name") for o in d.get("offices") or []]
        name = (d.get("location") or {}).get("name")
        job.locations = [o for o in offices if o] or ([name] if name else [])

    def _to_job(self, j: dict[str, Any]) -> Job:
        published = j.get("first_published") or j.get("updated_at")
        return Job(
            company=self.company,
            source_id=str(j["id"]),
            # internal_job_id is shared by every post of the same job; requisition_id is free text
            # that some companies fill with placeholders, so it isn't used.
            req_id=str(j["internal_job_id"]) if j.get("internal_job_id") else None,
            title=j["title"].strip(),
            url=j["absolute_url"],
            # Free-text location ("SF / NYC") is unreliable; offices from the detail call decide.
            locations=[],
            posted_at=datetime.fromisoformat(published) if published else None,
        )
