"""Oracle Recruiting Cloud "Candidate Experience" REST API (JPMorgan Chase, Texas Instruments, Oracle).

- The list call filters by a location facet, sorted newest first. The facet ID for a country differs
  per tenant, so it's looked up by name once (facetsList=LOCATIONS) and cached.
- The list only has a date (PostedDate); the detail call has the exact ExternalPostedStartDate.
- `site` only shapes the public job link; the API serves the same results for any site number.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

import httpx

from jobwatch.models import Job
from jobwatch.sources.base import Source, SourceError, html_to_text, request_json

PAGE_SIZE = 100


class OracleSource(Source):
    def __init__(self, company: str, host: str, site: str, country: str = "United States"):
        super().__init__(company)
        self.api = f"https://{host}/hcmRestApi/resources/latest"
        self.public = f"https://{host}/hcmUI/CandidateExperience/en/sites/{site}/job"
        self.site, self.country = site, country
        self._facet: int | None = None

    async def pages(self, client: httpx.AsyncClient) -> AsyncIterator[list[Job]]:
        facet = await self._country_facet(client)
        offset = 0
        while True:
            item = await self._search(client, f"selectedLocationsFacet={facet},limit={PAGE_SIZE},offset={offset}")
            reqs = item.get("requisitionList") or []
            if not reqs:
                return
            yield [self._to_job(r) for r in reqs if r.get("Id") and r.get("Title")]
            offset += len(reqs)
            if offset >= (item.get("TotalJobsCount") or 0):
                return

    async def enrich(self, client: httpx.AsyncClient, job: Job) -> None:
        data = await request_json(client, "GET", f"{self.api}/recruitingCEJobRequisitionDetails", params={
            "expand": "all", "onlyData": "true", "finder": f'ById;Id="{job.source_id}",siteNumber={self.site}'})
        d = (data.get("items") or [{}])[0]
        # Corporate description is the same company boilerplate on every posting.
        parts = (d.get(k) for k in ("ExternalDescriptionStr", "ExternalResponsibilitiesStr", "ExternalQualificationsStr"))
        job.description = "\n".join(t for p in parts if p and (t := html_to_text(p)))
        if start := d.get("ExternalPostedStartDate"):
            job.posted_at = datetime.fromisoformat(start)

    async def _country_facet(self, client: httpx.AsyncClient) -> int:
        if self._facet is None:
            item = await self._search(client, "facetsList=LOCATIONS,limit=1,offset=0", expand="locationsFacet")
            match = [f["Id"] for f in item.get("locationsFacet") or [] if f.get("Name") == self.country]
            if not match:
                raise SourceError(f"no {self.country!r} location facet on {self.api}")
            self._facet = match[0]
        return self._facet

    async def _search(self, client: httpx.AsyncClient, finder: str, expand: str = "requisitionList.secondaryLocations"
                      ) -> dict[str, Any]:
        data = await request_json(client, "GET", f"{self.api}/recruitingCEJobRequisitions", params={
            "onlyData": "true", "expand": expand,
            "finder": f"findReqs;siteNumber={self.site},{finder},sortBy=POSTING_DATES_DESC"})
        items = data.get("items") if isinstance(data, dict) else None
        if not items:
            raise SourceError(f"unexpected Oracle payload: {str(data)[:200]}")
        return items[0]

    def _to_job(self, r: dict[str, Any]) -> Job:
        secondary = [s["Name"] for s in r.get("secondaryLocations") or [] if s.get("Name")]
        posted = r.get("PostedDate")
        return Job(
            company=self.company,
            source_id=str(r["Id"]),
            title=r["Title"].strip(),
            url=f"{self.public}/{r['Id']}",
            locations=[loc for loc in [r.get("PrimaryLocation"), *secondary] if loc],
            posted_at=datetime.strptime(posted, "%Y-%m-%d").replace(tzinfo=UTC) if posted else None,
        )
