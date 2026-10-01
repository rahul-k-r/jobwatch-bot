"""Jibe careers sites (iCIMS-owned front end), e.g. careers.amd.com. List pages include full descriptions.

The iCIMS portal behind them (careers-<co>.icims.com) disallows all crawling in robots.txt, so only
the Jibe site is used. It asks for `crawl-delay: 5`, honored between page requests.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import datetime
from typing import Any

import httpx

from jobwatch.models import Job
from jobwatch.sources.base import Source, SourceError, html_to_text, request_json

PAGE_SIZE = 100


class JibeSource(Source):
    def __init__(self, company: str, host: str, params: dict[str, Any] | None = None, crawl_delay: float = 5):
        super().__init__(company)
        self.base = f"https://{host}"
        self.params = params or {}
        self.crawl_delay = crawl_delay

    async def pages(self, client: httpx.AsyncClient) -> AsyncIterator[list[Job]]:
        page, seen = 1, 0
        while True:
            if page > 1:
                await asyncio.sleep(self.crawl_delay)
            params = {**self.params, "page": page, "limit": PAGE_SIZE, "sortBy": "posted_date", "descending": "true"}
            data = await request_json(client, "GET", f"{self.base}/api/jobs", params=params)
            if not isinstance(data, dict) or not isinstance(data.get("jobs"), list):
                raise SourceError(f"unexpected Jibe payload: {str(data)[:200]}")
            jobs = [self._to_job(j["data"]) for j in data["jobs"] if j.get("data", {}).get("employment_type") != "INTERN"]
            total = data.get("totalCount") or 0
            if not data["jobs"]:
                # A premature empty page would end a seed early and later alert old jobs as new.
                if seen < total:
                    raise SourceError(f"empty page {page} after {seen} of {total}")
                return
            seen += len(data["jobs"])
            if jobs:
                yield jobs
            if seen >= total:
                return
            page += 1

    def _to_job(self, d: dict[str, Any]) -> Job:
        location = ", ".join(filter(None, [d.get("city"), d.get("state"), d.get("country")]))
        posted = d.get("posted_date")
        return Job(
            company=self.company,
            source_id=str(d["slug"]),
            req_id=d.get("req_id"),
            title=d["title"].strip(),
            url=f"{self.base}/careers-home/jobs/{d['slug']}?lang={d.get('language') or 'en-us'}",
            locations=[location] if location else [],
            # create_date is Jibe's ingestion time, not the requisition's, so no created/posted gap here.
            posted_at=datetime.strptime(posted, "%Y-%m-%dT%H:%M:%S%z") if posted else None,
            description="\n\n".join(html_to_text(d.get(k) or "") for k in ("description", "qualifications", "responsibilities")
                                    if d.get(k)),
        )
