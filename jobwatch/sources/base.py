from __future__ import annotations

import html
import re
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from typing import Any

import httpx

from jobwatch.models import Job


class SourceError(Exception):
    """A fetch failed or returned something unusable. Never means 'zero jobs'."""

    def __init__(self, message: str, retry_after: float | None = None):
        super().__init__(message)
        self.retry_after = retry_after


class Source(ABC):
    def __init__(self, company: str):
        self.company = company

    @abstractmethod
    def pages(self, client: httpx.AsyncClient) -> AsyncIterator[list[Job]]:
        """Yield pages of postings, newest first. The caller stops iterating once a page holds nothing new."""

    async def enrich(self, client: httpx.AsyncClient, job: Job) -> None:
        """Fill job.description (and anything else only a detail call provides). Default: no-op."""


async def request_json(client: httpx.AsyncClient, method: str, url: str, **kwargs: Any) -> Any:
    try:
        resp = await client.request(method, url, **kwargs)
    except httpx.HTTPError as e:
        raise SourceError(f"{method} {url}: {e!r}") from e
    if resp.status_code != 200:
        retry_after = resp.headers.get("Retry-After", "")
        raise SourceError(f"{method} {url}: HTTP {resp.status_code}",
                          retry_after=float(retry_after) if retry_after.isdigit() else None)
    try:
        return resp.json()
    except ValueError as e:
        # Seen on Microsoft: intermittent 200s with an empty body.
        raise SourceError(f"{method} {url}: non-JSON body ({len(resp.content)} bytes)") from e


_BLOCK_TAGS = re.compile(r"<\s*(br|/p|/li|/div|/h\d|/tr|li)\b[^>]*>", re.I)
_TAGS = re.compile(r"<[^>]+>")


def html_to_text(s: str) -> str:
    s = _BLOCK_TAGS.sub("\n", s)
    s = html.unescape(_TAGS.sub("", s)).replace("\xa0", " ")
    lines = (re.sub(r"[ \t]+", " ", line).strip() for line in s.splitlines())
    return "\n".join(line for line in lines if line)
