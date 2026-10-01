"""Ashby public posting API (official). Descriptions are always included (~1.2MB gzip for OpenAI)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime
from typing import Any

import httpx

from jobwatch.models import Job
from jobwatch.sources.base import Source, SourceError, html_to_text, request_json


class AshbySource(Source):
    def __init__(self, company: str, board: str):
        super().__init__(company)
        self.url = f"https://api.ashbyhq.com/posting-api/job-board/{board}"

    async def pages(self, client: httpx.AsyncClient) -> AsyncIterator[list[Job]]:
        data = await request_json(client, "GET", self.url)
        if not isinstance(data, dict) or not isinstance(data.get("jobs"), list):
            raise SourceError(f"unexpected Ashby payload: {str(data)[:200]}")
        jobs = [self._to_job(j) for j in data["jobs"]
                if j.get("isListed", True) and j.get("employmentType") != "Intern"]
        if jobs:
            yield jobs

    def _to_job(self, j: dict[str, Any]) -> Job:
        places = [(j.get("location"), j.get("address"))]
        places += [(s.get("location"), s.get("address")) for s in j.get("secondaryLocations") or []]
        return Job(
            company=self.company,
            source_id=j["id"],
            title=j["title"].strip(),
            url=j["jobUrl"],
            locations=[loc for name, address in places if (loc := _location(name, address))],
            posted_at=datetime.fromisoformat(j["publishedAt"]) if j.get("publishedAt") else None,
            description=j.get("descriptionPlain") or html_to_text(j.get("descriptionHtml") or ""),
        )


def _location(name: str | None, address: dict[str, Any] | None) -> str:
    """"San Francisco" + structured country -> "San Francisco, United States"."""
    country = ((address or {}).get("postalAddress") or {}).get("addressCountry") or ""
    name = name or ""
    return name if not country or country in name else ", ".join(filter(None, [name, country]))
