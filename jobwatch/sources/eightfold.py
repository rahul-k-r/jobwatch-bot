"""Eightfold careers API. Unauthenticated JSON; 10 results per page regardless of `num`.

`api="pcsx"` (Microsoft, Qualcomm) is the current API; `api="v2"` is the older /api/apply/v2 one,
for tenants that haven't enabled PCSX (Netflix answers PCSX with 403 "not enabled").
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any, Literal

import httpx

from jobwatch.models import Job
from jobwatch.sources.base import Source, SourceError, html_to_text, request_json


def _ts(v: int | str | None) -> datetime | None:
    return datetime.fromtimestamp(int(v), UTC) if v else None


class EightfoldSource(Source):
    def __init__(self, company: str, host: str, domain: str, params: dict[str, Any] | None = None,
                 api: Literal["pcsx", "v2"] = "pcsx"):
        super().__init__(company)
        self.base = f"https://{host}"
        self.domain = domain
        self.params = params or {}
        self.api = api

    async def pages(self, client: httpx.AsyncClient) -> AsyncIterator[list[Job]]:
        path = "/api/pcsx/search" if self.api == "pcsx" else "/api/apply/v2/jobs"
        start = 0
        while True:
            params = {**self.params, "domain": self.domain, "sort_by": "timestamp", "start": start}
            data = self._data(await request_json(client, "GET", self.base + path, params=params))
            positions = data.get("positions") or []
            if not positions:
                # A premature empty page would end a seed early and later alert old jobs as new.
                if start < (data.get("count") or 0):
                    raise SourceError(f"empty page at start={start} of {data.get('count')}")
                return
            yield [self._to_job(p) for p in positions]
            start += len(positions)

    async def enrich(self, client: httpx.AsyncClient, job: Job) -> None:
        if self.api == "pcsx":
            params = {"position_id": job.source_id, "domain": self.domain, "hl": "en"}
            body = await request_json(client, "GET", f"{self.base}/api/pcsx/position_details", params=params)
            job.description = html_to_text(self._data(body).get("jobDescription") or "")
        else:
            body = await request_json(client, "GET", f"{self.base}/api/apply/v2/jobs/{job.source_id}",
                                      params={"domain": self.domain})
            job.description = html_to_text((body if isinstance(body, dict) else {}).get("job_description") or "")

    def _to_job(self, p: dict[str, Any]) -> Job:
        if self.api == "v2":
            return Job(
                company=self.company,
                source_id=str(p["id"]),
                req_id=p.get("ats_job_id") or p.get("display_job_id"),
                title=p["name"].strip(),
                url=p.get("canonicalPositionUrl") or f"{self.base}/careers/job/{p['id']}",
                locations=p.get("locations") or [],
                # t_update changes on every edit, so it isn't a posting date.
                posted_at=_ts(p.get("t_create")),
            )
        return Job(
            company=self.company,
            source_id=str(p["id"]),
            req_id=p.get("atsJobId") or p.get("displayJobId"),
            title=p["name"].strip(),
            url=self.base + p["positionUrl"],
            locations=p.get("locations") or [],
            posted_at=_ts(p.get("postedTs")),
            created_at=_ts(p.get("creationTs")),
        )

    def _data(self, body: Any) -> dict[str, Any]:
        if self.api == "v2":
            if not isinstance(body, dict) or "positions" not in body:
                raise SourceError(f"unexpected Eightfold payload: {str(body)[:200]}")
            return body
        if not isinstance(body, dict) or body.get("status") != 200 or not isinstance(body.get("data"), dict):
            raise SourceError(f"unexpected Eightfold payload: {str(body)[:200]}")
        return body["data"]
