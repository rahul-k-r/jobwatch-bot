"""Lever public postings API (official).

The response is uncompressed and not sorted by date (Palantir: ~6MB per poll), so large
boards should get a longer per-source `interval_seconds` in config.yaml.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

import httpx

from jobwatch.models import Job
from jobwatch.sources.base import Source, SourceError, html_to_text, request_json


class LeverSource(Source):
    def __init__(self, company: str, board: str, region: str = "global"):
        super().__init__(company)
        host = "api.eu.lever.co" if region == "eu" else "api.lever.co"
        self.url = f"https://{host}/v0/postings/{board}"

    async def pages(self, client: httpx.AsyncClient) -> AsyncIterator[list[Job]]:
        data = await request_json(client, "GET", self.url, params={"mode": "json"})
        if not isinstance(data, list):
            raise SourceError(f"unexpected Lever payload: {str(data)[:200]}")
        jobs = [self._to_job(p) for p in data if "intern" not in (p.get("categories") or {}).get("commitment", "").lower()]
        if jobs:
            yield jobs

    def _to_job(self, p: dict[str, Any]) -> Job:
        cats = p.get("categories") or {}
        locs = cats.get("allLocations") or ([cats["location"]] if cats.get("location") else [])
        if p.get("country") == "US":
            # Location text is free-form ("Denver, CO"); the ISO country field is reliable.
            locs = [f"{loc}, United States" for loc in locs] or ["United States"]
        sections = [p.get("descriptionPlain") or ""]
        sections += [f"{s.get('text', '')}\n{html_to_text(s.get('content') or '')}" for s in p.get("lists") or []]
        sections.append(p.get("additionalPlain") or "")
        return Job(
            company=self.company,
            source_id=p["id"],
            title=p["text"].strip(),
            url=p["hostedUrl"],
            locations=locs,
            posted_at=datetime.fromtimestamp(p["createdAt"] / 1000, UTC) if p.get("createdAt") else None,
            description="\n".join(s for s in sections if s.strip()),
        )
