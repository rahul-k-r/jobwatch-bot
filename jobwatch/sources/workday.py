"""Workday CXS API (/wday/cxs/{tenant}/{site}/jobs), shared by many companies.

- Newest-first only when searchText is empty (non-empty sorts by relevance); no sort param exists.
- limit max is 20; offsets >= 2000 silently return page 1 again.
- Facet keys/IDs are tenant-specific, and some tenants silently ignore unknown facets,
  so verify `total` drops when adding one.
- List `locationsText` is free text ("Washington - Bellevue", "4 Locations"), so locations
  come from the detail call and the list reports them as unknown.
"""

from __future__ import annotations

import html
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

import httpx

from jobwatch.models import Job
from jobwatch.sources.base import Source, SourceError, html_to_text, request_json

PAGE_SIZE = 20
MAX_OFFSET = 2000


class WorkdaySource(Source):
    def __init__(self, company: str, host: str, tenant: str, site: str, facets: dict[str, list[str]] | None = None):
        super().__init__(company)
        self.api = f"https://{host}/wday/cxs/{tenant}/{site}"
        self.public = f"https://{host}/{site}"
        self.facets = facets or {}

    async def pages(self, client: httpx.AsyncClient) -> AsyncIterator[list[Job]]:
        offset, total = 0, 0
        while offset < MAX_OFFSET:
            body = {"appliedFacets": self.facets, "limit": PAGE_SIZE, "offset": offset, "searchText": ""}
            data = await request_json(client, "POST", f"{self.api}/jobs", json=body)
            total = total or data.get("total") or 0  # only reported on the first page
            postings = data.get("jobPostings") or []
            if not postings:
                if offset < min(total, MAX_OFFSET):
                    raise SourceError(f"empty page at offset={offset} of {total}")
                return
            # Intel has served entries without a title; skip rather than fail the whole poll.
            yield [self._to_job(p) for p in postings if p.get("title") and p.get("externalPath")]
            offset += len(postings)

    async def enrich(self, client: httpx.AsyncClient, job: Job) -> None:
        data = await request_json(client, "GET", self.api + job.url.removeprefix(self.public))
        info = data.get("jobPostingInfo") or {}
        # Some tenants (Salesforce) double-escape entities: "&amp;#xa;".
        job.description = html_to_text(html.unescape(info.get("jobDescription") or ""))
        country = (info.get("country") or {}).get("descriptor", "")
        location = info.get("location") or ""
        primary = location if country in location else ", ".join(filter(None, [location, country]))
        job.locations = [primary, *info.get("additionalLocations", [])] if primary else []
        job.req_id = info.get("jobReqId") or job.req_id
        if start := info.get("startDate"):
            job.posted_at = datetime.strptime(start, "%Y-%m-%d").replace(tzinfo=UTC)

    def _to_job(self, p: dict[str, Any]) -> Job:
        path = p["externalPath"]
        tail = path.rsplit("/", 1)[-1]
        return Job(
            company=self.company,
            # "Title-Slug_JR361902-1" -> "JR361902-1": the slug can go stale when titles are edited.
            source_id=tail.rsplit("_", 1)[-1] if "_" in tail else path,
            # Intel prepends "Spotlight Job"; the req ID is the field with digits.
            req_id=next((b for b in p.get("bulletFields") or [] if any(c.isdigit() for c in b)), None),
            title=p["title"].strip(),
            url=self.public + path,
        )
