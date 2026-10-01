import json

import httpx
import pytest
import respx
from conftest import load_fixture

from jobwatch.sources.amazon import AmazonSource
from jobwatch.sources.apple import AppleSource
from jobwatch.sources.base import SourceError
from jobwatch.sources.ibm import IBMSource
from jobwatch.sources.workday import WorkdaySource


async def collect(src):
    async with httpx.AsyncClient() as client:
        return [page async for page in src.pages(client)]


async def enrich(src, job):
    async with httpx.AsyncClient() as client:
        await src.enrich(client, job)


# --- Amazon -----------------------------------------------------------------

AMAZON = "https://www.amazon.jobs/en/search.json"


@respx.mock
async def test_amazon_maps_jobs_with_description_and_stops_at_hits():
    body = load_fixture("amazon_search.json") | {"hits": 3}
    route = respx.get(AMAZON).mock(return_value=httpx.Response(200, json=body))
    [page] = await collect(AmazonSource("Amazon", {"category[]": "software-development", "country": "USA"}))

    assert route.call_count == 1
    params = route.calls[0].request.url.params
    assert params["sort"] == "recent" and params["result_limit"] == "100" and params["category[]"] == "software-development"
    job = page[1]
    assert job.key == "Amazon:10560727"
    assert job.title == "Software Development Engineer, Consumer Domains"
    assert job.url == "https://www.amazon.jobs/en/jobs/10560727/software-development-engineer-consumer-domains"
    assert job.locations == ["US, NJ, Newark"]
    assert job.posted_at.date().isoformat() == "2026-09-25"
    assert "Basic Qualifications\n- 1+ years of non-internship" in job.description
    assert page[0].title.endswith("Inter-Satellite Link")  # trailing space stripped


@respx.mock
async def test_amazon_error_field_raises():
    respx.get(AMAZON).mock(return_value=httpx.Response(200, json={"error": "Result limit cannot be greater than 100"}))
    with pytest.raises(SourceError):
        await collect(AmazonSource("Amazon"))


# --- Apple ------------------------------------------------------------------

APPLE = "https://jobs.apple.com/api/v1/search"


@respx.mock
async def test_apple_merges_location_rows_and_sends_required_format():
    route = respx.post(APPLE).mock(side_effect=[
        httpx.Response(200, json=load_fixture("apple_search.json")),
        httpx.Response(200, json={"res": {"totalRecords": 1300, "searchResults": []}}),
    ])
    [page] = await collect(AppleSource("Apple", teams=["teamsAndSubTeams-SFTWR"]))

    sent = json.loads(route.calls[0].request.content)
    assert sent["sort"] == "newest" and "format" in sent
    assert sent["filters"] == {"locations": ["postLocation-USA"], "teams": [{"team": "teamsAndSubTeams-SFTWR"}]}
    assert json.loads(route.calls[1].request.content)["page"] == 2
    assert [j.source_id for j in page] == ["200651071", "200685421", "200685976"]
    designer = page[2]
    assert designer.locations == ["Culver City, United States of America", "New York City, United States of America"]
    assert designer.url == "https://jobs.apple.com/en-us/details/200685976-0670/lead-designer-design-systems-structures"
    assert page[0].title == "Senior Machine Learning Engineer, NLP, Input Experience"  # double space collapsed
    assert page[0].posted_at.isoformat().startswith("2026-09-27T01:01:18")


APPLE_ZERO = {"res": {"totalRecords": 0, "searchResults": []}}


@respx.mock
async def test_apple_persistent_zero_results_is_an_error(monkeypatch):
    monkeypatch.setattr("jobwatch.sources.apple.RETRY_DELAY", 0)
    respx.post(APPLE).mock(return_value=httpx.Response(200, json=APPLE_ZERO))
    with pytest.raises(SourceError, match="0 results"):
        await collect(AppleSource("Apple", teams=["teamsAndSubTeams-SFTWR"]))


@respx.mock
async def test_apple_transient_zero_mid_pagination_is_retried_not_treated_as_end(monkeypatch):
    # Observed live: ~1 in 5 requests randomly return totalRecords 0.
    monkeypatch.setattr("jobwatch.sources.apple.RETRY_DELAY", 0)
    page = load_fixture("apple_search.json")
    page["res"]["totalRecords"] = 8
    route = respx.post(APPLE).mock(side_effect=[
        httpx.Response(200, json=page),
        httpx.Response(200, json=APPLE_ZERO),
        httpx.Response(200, json=page),
    ])
    pages = await collect(AppleSource("Apple", teams=["teamsAndSubTeams-SFTWR"]))
    assert len(pages) == 2  # stops once 8 rows are read, without a trailing request
    assert route.call_count == 3


@respx.mock
async def test_apple_enrich_joins_sections():
    respx.get("https://jobs.apple.com/api/v1/jobDetails/200685898").mock(
        return_value=httpx.Response(200, json=load_fixture("apple_detail.json")))
    from jobwatch.models import Job
    job = Job(company="Apple", source_id="200685898", title="t", url="u")
    await enrich(AppleSource("Apple", teams=[]), job)
    assert "Minimum Qualifications\nYou care about supporting others" in job.description
    assert "Preferred Qualifications\nExtensive experience with XCTest" in job.description


# --- IBM --------------------------------------------------------------------

IBM = "https://www-api.ibm.com/search/api/v2"


@respx.mock
async def test_ibm_maps_hits_skips_internships_and_stops_at_total():
    body = load_fixture("ibm_search.json")
    body["hits"]["total"]["value"] = 3
    route = respx.post(IBM).mock(return_value=httpx.Response(200, json=body))
    [page] = await collect(IBMSource("IBM"))

    assert route.call_count == 1
    sent = json.loads(route.calls[0].request.content)
    assert sent["sort"] == [{"dcdate": "desc"}] and sent["size"] == 100 and sent["_source"]
    assert [j.source_id for j in page] == ["134607", "134599"]
    job = page[0]
    assert job.url == "https://careers.ibm.com/careers/JobDetail?jobId=134607"
    assert job.locations == ["BATON ROUGE, US, United States"]
    assert job.description.startswith("A career in IBM Consulting")


# --- Workday ----------------------------------------------------------------

HOST = "salesforce.wd12.myworkdayjobs.com"
WD_LIST = f"https://{HOST}/wday/cxs/salesforce/External_Career_Site/jobs"


def workday():
    return WorkdaySource("Salesforce", HOST, "salesforce", "External_Career_Site",
                         facets={"jobFamilyGroup": ["14fa3452ec7c1011f90d0002a2100000"]})


@respx.mock
async def test_workday_list_ids_req_ids_and_request_body():
    route = respx.post(WD_LIST).mock(side_effect=[
        httpx.Response(200, json=load_fixture("workday_search.json")),
        httpx.Response(200, json={"total": 0, "jobPostings": []}),
    ])
    [page] = await collect(workday())

    sent = json.loads(route.calls[0].request.content)
    assert sent == {"appliedFacets": {"jobFamilyGroup": ["14fa3452ec7c1011f90d0002a2100000"]}, "limit": 20,
                    "offset": 0, "searchText": ""}
    assert json.loads(route.calls[1].request.content)["offset"] == 3
    assert [j.source_id for j in page] == ["JR361861", "JR358244", "JR361902-1"]
    assert page[1].req_id == "JR358244"  # skips "Spotlight Job"
    assert page[2].url == f"https://{HOST}/External_Career_Site/job/California---Remote/Sr-Solution-Engineer_JR361902-1"
    assert all(j.locations == [] for j in page)  # unknown until the detail call


@respx.mock
async def test_workday_skips_postings_without_title_or_path():
    # Seen live on Intel: an entry with no "title" crashed the whole poll.
    body = load_fixture("workday_search.json")
    body["jobPostings"].insert(0, {"externalPath": "/job/x/Promo_JR1", "bulletFields": ["JR1"]})
    body["jobPostings"].insert(1, {"title": "No path"})
    respx.post(WD_LIST).mock(side_effect=[
        httpx.Response(200, json=body),
        httpx.Response(200, json={"total": 0, "jobPostings": []}),
    ])
    [page] = await collect(workday())
    assert [j.source_id for j in page] == ["JR361861", "JR358244", "JR361902-1"]


@respx.mock
async def test_workday_empty_page_before_total_is_an_error():
    first = load_fixture("workday_search.json") | {"total": 97}
    respx.post(WD_LIST).mock(side_effect=[
        httpx.Response(200, json=first),
        httpx.Response(200, json={"total": 0, "jobPostings": []}),  # later pages always report total 0
    ])
    with pytest.raises(SourceError, match="empty page"):
        await collect(workday())


@respx.mock
async def test_workday_stops_at_offset_cap():
    posting = load_fixture("workday_search.json")["jobPostings"][0]
    full_page = {"total": 0, "jobPostings": [posting] * 20}
    route = respx.post(WD_LIST).mock(return_value=httpx.Response(200, json=full_page))
    pages = await collect(workday())
    assert len(pages) == 100 and route.call_count == 100  # 100 * 20 = 2000


@respx.mock
async def test_workday_enrich_fills_locations_description_and_date():
    detail = f"https://{HOST}/wday/cxs/salesforce/External_Career_Site/job/California---Remote/Sr-Solution-Engineer_JR361902-1"
    respx.post(WD_LIST).mock(side_effect=[
        httpx.Response(200, json=load_fixture("workday_search.json")),
        httpx.Response(200, json={"total": 0, "jobPostings": []}),
    ])
    respx.get(detail).mock(return_value=httpx.Response(200, json=load_fixture("workday_detail.json")))
    src = workday()
    job = (await collect(src))[0][2]
    await enrich(src, job)

    assert job.locations == ["California - Remote, United States of America", "Washington - Bellevue"]
    assert job.req_id == "JR361902"
    assert job.posted_at.date().isoformat() == "2026-09-25"
    assert "&#xa;" not in job.description and "&amp;" not in job.description
    assert "$134,750 - $180,250" in job.description
