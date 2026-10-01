import httpx
import pytest
import respx

from jobwatch.sources.base import SourceError
from jobwatch.sources.jibe import JibeSource

API = "https://careers.amd.com/api/jobs"


def posting(slug, title="AI Research Scientist", employment_type="FULL_TIME"):
    return {"data": {
        "slug": slug, "req_id": slug, "language": "en-us", "title": f" {title} ", "employment_type": employment_type,
        "city": "Bellevue", "state": "Washington", "country": "United States",
        "posted_date": "2026-09-30T21:21:00+0000", "create_date": "2026-09-30T21:22:39+0000",
        "description": "<p>Advance AI.</p>", "qualifications": "<ul><li>MS in CS</li></ul>", "responsibilities": "",
    }}


def page(jobs, total):
    return httpx.Response(200, json={"jobs": jobs, "totalCount": total})


def source():
    return JibeSource("AMD", "careers.amd.com", {"country": "United States"}, crawl_delay=0)


async def collect(src):
    async with httpx.AsyncClient() as client:
        return [p async for p in src.pages(client)]


@respx.mock
async def test_maps_postings_and_paginates_to_total():
    route = respx.get(API).mock(side_effect=[page([posting("1"), posting("2")], 3),
                                             page([posting("3", employment_type="INTERN")], 3)])
    pages = await collect(source())

    assert [[j.source_id for j in p] for p in pages] == [["1", "2"]]  # intern dropped, stops at total
    job = pages[0][0]
    assert job.key == "AMD:1" and job.req_id == "1" and job.title == "AI Research Scientist"
    assert job.url == "https://careers.amd.com/careers-home/jobs/1?lang=en-us"
    assert job.locations == ["Bellevue, Washington, United States"]
    assert job.posted_at.isoformat() == "2026-09-30T21:21:00+00:00" and job.created_at is None
    assert job.description == "Advance AI.\n\nMS in CS"
    first, second = (dict(c.request.url.params) for c in route.calls)
    assert first["page"] == "1" and first["sortBy"] == "posted_date" and first["country"] == "United States"
    assert second["page"] == "2"


@respx.mock
async def test_empty_page_before_total_is_an_error():
    respx.get(API).mock(side_effect=[page([posting("1")], 500), page([], 500)])
    with pytest.raises(SourceError, match="empty page"):
        await collect(source())


@respx.mock
async def test_unexpected_payload_is_an_error():
    respx.get(API).mock(return_value=httpx.Response(200, json={"error": "nope"}))
    with pytest.raises(SourceError):
        await collect(source())


@respx.mock
async def test_waits_crawl_delay_between_pages(monkeypatch):
    slept = []

    async def fake_sleep(s):
        slept.append(s)

    monkeypatch.setattr("jobwatch.sources.jibe.asyncio.sleep", fake_sleep)
    respx.get(API).mock(side_effect=[page([posting("1")], 2), page([posting("2")], 2)])
    await collect(JibeSource("AMD", "careers.amd.com"))
    assert slept == [5]
