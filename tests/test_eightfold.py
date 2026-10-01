import httpx
import pytest
import respx
from conftest import load_fixture

from jobwatch.sources.base import SourceError
from jobwatch.sources.eightfold import EightfoldSource

SEARCH = "https://apply.careers.microsoft.com/api/pcsx/search"
DETAIL = "https://apply.careers.microsoft.com/api/pcsx/position_details"
EMPTY_PAGE = {"status": 200, "data": {"count": 3, "positions": []}}


def search_page(count=3):
    body = load_fixture("microsoft_search.json")
    body["data"]["count"] = count
    return httpx.Response(200, json=body)


def source():
    return EightfoldSource("Microsoft", "apply.careers.microsoft.com", "microsoft.com", {"location": "United States"})


async def collect(src):
    async with httpx.AsyncClient() as client:
        return [page async for page in src.pages(client)]


@respx.mock
async def test_maps_positions_and_paginates_until_empty():
    route = respx.get(SEARCH).mock(side_effect=[search_page(), httpx.Response(200, json=EMPTY_PAGE)])
    pages = await collect(source())

    assert len(pages) == 1
    job = pages[0][2]
    assert job.key == "Microsoft:1970393557006899"
    assert job.req_id == "200057588"
    assert job.title == "Software Engineer II - Operating Systems"
    assert job.url == "https://apply.careers.microsoft.com/careers/job/1970393557006899"
    assert job.locations == ["United States, Washington, Redmond"]
    assert job.created_at < job.posted_at
    first, second = (dict(c.request.url.params) for c in route.calls)
    assert first["sort_by"] == "timestamp" and first["start"] == "0" and first["location"] == "United States"
    assert second["start"] == "3"


@respx.mock
async def test_empty_page_before_reported_count_is_an_error():
    premature = {"status": 200, "data": {"count": 367, "positions": []}}
    respx.get(SEARCH).mock(side_effect=[search_page(count=367), httpx.Response(200, json=premature)])
    with pytest.raises(SourceError, match="empty page"):
        await collect(source())


@respx.mock
async def test_empty_200_body_is_an_error_not_zero_jobs():
    respx.get(SEARCH).mock(return_value=httpx.Response(200, content=b""))
    with pytest.raises(SourceError, match="non-JSON"):
        await collect(source())


@respx.mock
async def test_rate_limit_carries_retry_after():
    respx.get(SEARCH).mock(return_value=httpx.Response(429, headers={"Retry-After": "120"}))
    with pytest.raises(SourceError) as exc:
        await collect(source())
    assert exc.value.retry_after == 120


@respx.mock
async def test_error_payload_raises():
    respx.get(SEARCH).mock(return_value=httpx.Response(200, json={"status": 403, "error": {"message": "nope"}}))
    with pytest.raises(SourceError):
        await collect(source())


@respx.mock
async def test_enrich_fills_plain_text_description():
    respx.get(SEARCH).mock(side_effect=[search_page(), httpx.Response(200, json=EMPTY_PAGE)])
    respx.get(DETAIL).mock(return_value=httpx.Response(200, json=load_fixture("microsoft_detail.json")))
    src = source()
    job = (await collect(src))[0][2]
    async with httpx.AsyncClient() as client:
        await src.enrich(client, job)

    assert "2+ years technical engineering experience" in job.description
    assert "<" not in job.description
    assert "Required Qualifications\n" in job.description


# Older v2 API (Netflix): no status wrapper, string timestamps, canonical URL, description via /jobs/{id}.
V2_SEARCH = "https://explore.jobs.netflix.net/api/apply/v2/jobs"
V2_POSITION = {
    "id": "790299000001", "name": "Software Engineering L5, Open Connect Platform",
    "locations": ["Remote, United States"], "t_create": "1790208000", "t_update": "1790208000",
    "ats_job_id": "JR12345", "job_description": "",
    "canonicalPositionUrl": "https://explore.jobs.netflix.net/careers/job/790299000001",
}


def v2_source():
    return EightfoldSource("Netflix", "explore.jobs.netflix.net", "netflix.com", {"location": "United States"}, api="v2")


@respx.mock
async def test_v2_maps_positions_and_paginates():
    route = respx.get(V2_SEARCH).mock(side_effect=[
        httpx.Response(200, json={"count": 1, "positions": [V2_POSITION]}),
        httpx.Response(200, json={"count": 1, "positions": []}),
    ])
    [[job]] = await collect(v2_source())

    assert job.key == "Netflix:790299000001"
    assert job.req_id == "JR12345"
    assert job.url == "https://explore.jobs.netflix.net/careers/job/790299000001"
    assert job.locations == ["Remote, United States"]
    # t_update moves on every edit, so it isn't used as a posting date (would fake repost gaps).
    assert job.posted_at.isoformat().startswith("2026-09-24") and job.created_at is None
    first = dict(route.calls[0].request.url.params)
    assert first["domain"] == "netflix.com" and first["location"] == "United States" and first["start"] == "0"


@respx.mock
async def test_v2_error_payload_and_premature_empty_page_raise():
    respx.get(V2_SEARCH).mock(return_value=httpx.Response(200, json={"message": "PCSX is not enabled for this user."}))
    with pytest.raises(SourceError):
        await collect(v2_source())
    respx.get(V2_SEARCH).mock(return_value=httpx.Response(200, json={"count": 40, "positions": []}))
    with pytest.raises(SourceError, match="empty page"):
        await collect(v2_source())


@respx.mock
async def test_v2_enrich_fetches_description():
    respx.get(f"{V2_SEARCH}/790299000001").mock(
        return_value=httpx.Response(200, json={**V2_POSITION, "job_description": "<p>Build <b>CDN</b> software.</p>"}))
    src = v2_source()
    job = src._to_job(V2_POSITION)
    async with httpx.AsyncClient() as client:
        await src.enrich(client, job)
    assert job.description == "Build CDN software."
