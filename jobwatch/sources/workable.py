"""Workable public widget API (the JSON behind embedded job boards), with descriptions."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

import httpx

from jobwatch.models import Job
from jobwatch.sources.base import Source, SourceError, html_to_text, request_json


class WorkableSource(Source):
    def __init__(self, company: str, board: str):
        super().__init__(company)
        self.url = f"https://apply.workable.com/api/v1/widget/accounts/{board}"

    async def pages(self, client: httpx.AsyncClient) -> AsyncIterator[list[Job]]:
        data = await request_json(client, "GET", self.url, params={"details": "true"})
        if not isinstance(data, dict) or not isinstance(data.get("jobs"), list):
            raise SourceError(f"unexpected Workable payload: {str(data)[:200]}")
        if data["jobs"]:
            yield [self._to_job(j) for j in data["jobs"]]

    def _to_job(self, j: dict[str, Any]) -> Job:
        places = j.get("locations") or [j]
        locs = [", ".join(filter(None, [p.get("city"), p.get("region") or p.get("state"), p.get("country")]))
                for p in places]
        published = j.get("published_on") or j.get("created_at")
        return Job(
            company=self.company,
            source_id=j["shortcode"],
            title=j["title"].strip(),
            url=j["url"],
            locations=[loc for loc in locs if loc],
            posted_at=datetime.strptime(published, "%Y-%m-%d").replace(tzinfo=UTC) if published else None,
            description=html_to_text(j.get("description") or ""),
        )
