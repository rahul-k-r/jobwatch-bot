"""IBM careers search (Elasticsearch-style API behind ibm.com/careers/search).

`body` carries the full posting text; job detail pages sit behind an AWS WAF challenge,
so there is no enrich step. Dates are day-granular.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

import httpx

from jobwatch.models import Job
from jobwatch.sources.base import Source, SourceError, request_json

URL = "https://www-api.ibm.com/search/api/v2"
PAGE_SIZE = 100  # API max
# _source must be listed explicitly; `true` / ["*"] are rejected.
_FIELDS = ["title", "url", "dcdate", "body", "field_text_01", "field_keyword_05", "field_keyword_08",
           "field_keyword_18", "field_keyword_19"]


class IBMSource(Source):
    def __init__(self, company: str, category: str = "Software Engineering", country: str = "United States"):
        super().__init__(company)
        self.must = [{"term": {"field_keyword_05": country}}, {"term": {"field_keyword_08": category}}]

    async def pages(self, client: httpx.AsyncClient) -> AsyncIterator[list[Job]]:
        offset = 0
        while True:
            body = {
                "appId": "careers", "scopes": ["careers2"], "lang": "zz", "localeSelector": {}, "p": 1,
                "sm": {"query": "", "lang": "zz"},
                "query": {"bool": {"must": self.must}},
                "sort": [{"dcdate": "desc"}], "size": PAGE_SIZE, "from": offset, "_source": _FIELDS,
            }
            data = await request_json(client, "POST", URL, json=body)
            try:
                hits = data["hits"]["hits"]
                total = data["hits"]["total"]["value"]
            except (KeyError, TypeError) as e:
                raise SourceError(f"unexpected IBM payload: {str(data)[:200]}") from e
            if not hits:
                if offset < total:
                    raise SourceError(f"empty page at from={offset} of {total}")
                return
            # Internships aren't always labeled in the title; the level field is reliable.
            yield [self._to_job(h["_source"]) for h in hits if h["_source"].get("field_keyword_18") != "Internship"]
            offset += len(hits)
            if offset >= total:
                return

    def _to_job(self, s: dict[str, Any]) -> Job:
        return Job(
            company=self.company,
            source_id=str(s["field_text_01"]),
            title=s["title"].strip(),
            url=s["url"],
            locations=[f"{s.get('field_keyword_19', '')}, {s.get('field_keyword_05', '')}".strip(", ")],
            posted_at=datetime.strptime(s["dcdate"], "%Y-%m-%d").replace(tzinfo=UTC) if s.get("dcdate") else None,
            description=s.get("body"),
        )
