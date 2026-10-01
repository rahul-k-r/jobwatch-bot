"""Google careers feed.xml: a full snapshot of every Google job (~3.4k, ~20MB raw / ~4MB gzip).

The live results pages are disallowed in robots.txt, and Google's ToS forbid automated access
that violates robots.txt, so this uses the permitted feed only. The feed is regenerated roughly
every 20-25 min; conditional GETs make polling it every few minutes a cheap 304 in between.

`published` is the last-modified time, not creation, so there's no created/posted repost gap.
"""

from __future__ import annotations

import io
import xml.etree.ElementTree as ET
from collections.abc import AsyncIterator
from datetime import datetime

import httpx

from jobwatch.models import Job
from jobwatch.sources.base import Source, SourceError, html_to_text

FEED = "https://www.google.com/about/careers/applications/jobs/feed.xml"


class GoogleSource(Source):
    def __init__(self, company: str, categories: list[str] | None = None, countries: list[str] | None = None,
                 job_types: list[str] | None = None):
        super().__init__(company)
        # [] = every category (title rules decide instead).
        self.categories = set(categories) if categories is not None else {"SOFTWARE_ENGINEERING"}
        self.countries = set(countries or ["USA"])
        self.job_types = set(job_types or ["FULL_TIME"])
        self.last_modified: str | None = None

    async def pages(self, client: httpx.AsyncClient) -> AsyncIterator[list[Job]]:
        headers = {"If-Modified-Since": self.last_modified} if self.last_modified else {}
        try:
            resp = await client.get(FEED, headers=headers)
        except httpx.HTTPError as e:
            raise SourceError(f"GET {FEED}: {e!r}") from e
        if resp.status_code == 304:
            return
        if resp.status_code != 200:
            raise SourceError(f"GET {FEED}: HTTP {resp.status_code}")
        jobs = self._parse(resp.content)
        if not jobs:
            raise SourceError(f"feed had no matching jobs ({len(resp.content)} bytes); format may have changed")
        self.last_modified = resp.headers.get("Last-Modified")
        yield jobs

    def _parse(self, content: bytes) -> list[Job]:
        jobs = []
        try:
            for _, el in ET.iterparse(io.BytesIO(content), events=("end",)):
                if el.tag == "job":
                    if job := self._to_job(el):
                        jobs.append(job)
                    el.clear()
        except ET.ParseError as e:
            raise SourceError(f"feed XML parse error: {e}") from e
        return jobs

    def _to_job(self, el: ET.Element) -> Job | None:
        if el.findtext("jobtype") not in self.job_types:
            return None
        if self.categories and not self.categories & {c.text for c in el.iter("category")}:
            return None
        locs = [(loc.findtext("city"), loc.findtext("state"), loc.findtext("country")) for loc in el.iter("location")]
        # No locations listed -> keep; the pipeline treats unknown location as possibly US.
        if locs and not any(country in self.countries for _, _, country in locs):
            return None
        published = el.findtext("published")
        return Job(
            company=self.company,
            source_id=el.findtext("jobid"),
            title=(el.findtext("title") or "").strip(),
            url=el.findtext("url") or "",
            locations=[", ".join(filter(None, loc)) for loc in locs],
            posted_at=datetime.fromisoformat(published.replace("Z", "+00:00")) if published else None,
            description=html_to_text(el.findtext("description") or ""),
        )
