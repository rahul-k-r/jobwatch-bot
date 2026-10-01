import httpx
import pytest
import respx
from conftest import FIXTURES

from jobwatch.sources.base import SourceError
from jobwatch.sources.google import FEED, GoogleSource

FEED_XML = (FIXTURES / "google_feed.xml").read_bytes()
LAST_MODIFIED = "Tue, 29 Sep 2026 13:55:15 GMT"


async def collect(src):
    async with httpx.AsyncClient() as client:
        return [page async for page in src.pages(client)]


@respx.mock
async def test_filters_to_us_full_time_software_jobs():
    respx.get(FEED).mock(return_value=httpx.Response(200, content=FEED_XML))
    [page] = await collect(GoogleSource("Google"))

    # dropped: Zurich-only, INTERN, PROGRAM_MANAGEMENT; kept: India+US combo and the no-location remote job
    assert [j.source_id for j in page] == ["135021631578743494", "133023244499198662", "107980810614645446"]
    staff = page[0]
    assert staff.key == "Google:135021631578743494"
    assert staff.url == "https://careers.google.com/jobs/results/135021631578743494-staff-software-engineer/"
    assert staff.locations == ["Mountain View, CA, USA"]
    assert staff.posted_at.isoformat().startswith("2026-09-25T20:36:38")
    assert "8 years of experience in software development." in staff.description
    assert "<" not in staff.description
    assert page[1].locations == ["Bangalore, KA, India", "San Francisco, CA, USA"]
    assert page[2].locations == []


@respx.mock
async def test_empty_categories_keeps_every_category():
    # Customer Engineers sit under SALES_OPERATIONS, DeepMind research scientists under INFORMATION_TECHNOLOGY.
    respx.get(FEED).mock(return_value=httpx.Response(200, content=FEED_XML))
    [page] = await collect(GoogleSource("Google", categories=[]))
    assert len(page) == 4
    assert "135021631578743494" in [j.source_id for j in page]


@respx.mock
async def test_conditional_get_makes_unchanged_feed_a_no_op():
    route = respx.get(FEED).mock(side_effect=[
        httpx.Response(200, content=FEED_XML, headers={"Last-Modified": LAST_MODIFIED}),
        httpx.Response(304),
    ])
    src = GoogleSource("Google")
    assert len(await collect(src)) == 1
    assert await collect(src) == []
    assert "If-Modified-Since" not in route.calls[0].request.headers
    assert route.calls[1].request.headers["If-Modified-Since"] == LAST_MODIFIED


@respx.mock
@pytest.mark.parametrize("response, match", [
    (httpx.Response(200, content=b"<html>not a feed"), "parse error"),
    (httpx.Response(200, content=b"<jobs><updated>x</updated></jobs>"), "no matching jobs"),
    (httpx.Response(503), "HTTP 503"),
])
async def test_bad_feed_is_an_error_and_not_cached(response, match):
    respx.get(FEED).mock(return_value=response)
    src = GoogleSource("Google")
    with pytest.raises(SourceError, match=match):
        await collect(src)
    assert src.last_modified is None
