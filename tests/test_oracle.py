"""Oracle Recruiting Cloud (Candidate Experience) adapter: JPMorgan Chase, Texas Instruments, Oracle."""

import httpx
import pytest
import respx

from jobwatch.models import Job
from jobwatch.sources import build_source
from jobwatch.sources.base import SourceError
from jobwatch.sources.oracle import OracleSource

HOST = "edbz.fa.us2.oraclecloud.com"
LIST = f"https://{HOST}/hcmRestApi/resources/latest/recruitingCEJobRequisitions"
DETAIL = f"https://{HOST}/hcmRestApi/resources/latest/recruitingCEJobRequisitionDetails"


def page(reqs, total=3, facets=None):
    item = {"TotalJobsCount": total, "requisitionList": reqs}
    if facets is not None:
        item["locationsFacet"] = facets
    return httpx.Response(200, json={"items": [item]})


def req(id_, title="Software Engineer", loc="Dallas, TX, United States", secondary=()):
    return {"Id": id_, "Title": title, "PostedDate": "2026-10-13", "PrimaryLocation": loc,
            "PrimaryLocationCountry": "US", "secondaryLocations": [{"Name": s} for s in secondary]}


async def collect(src):
    async with httpx.AsyncClient() as client:
        return [p async for p in src.pages(client)]


def finder(call):
    return call.request.url.params["finder"]


@respx.mock
async def test_looks_up_the_country_facet_once_then_pages_newest_first_within_it():
    route = respx.get(LIST).mock(side_effect=[
        page([], facets=[{"Id": 7, "Name": "TX, United States"}, {"Id": 300000000361862, "Name": "United States"}]),
        page([req("1"), req("2", secondary=["Austin, TX, United States"])]),
        page([req("3")]),
        page([]),
    ])
    src = OracleSource("Texas Instruments", HOST, "CX")
    pages = await collect(src)

    assert "facetsList=LOCATIONS" in finder(route.calls[0])
    assert "selectedLocationsFacet=300000000361862" in finder(route.calls[1])
    assert "sortBy=POSTING_DATES_DESC" in finder(route.calls[1]) and "offset=0" in finder(route.calls[1])
    assert "offset=2" in finder(route.calls[2])
    [first, second] = pages
    job = first[1]
    assert job.key == "Texas Instruments:2"
    assert job.url == f"https://{HOST}/hcmUI/CandidateExperience/en/sites/CX/job/2"
    assert job.locations == ["Dallas, TX, United States", "Austin, TX, United States"]
    assert job.posted_at.isoformat() == "2026-10-13T00:00:00+00:00"  # date only until enrich
    assert [j.source_id for j in second] == ["3"]

    route.reset()
    route.side_effect = [page([req("4")]), page([])]
    await collect(src)
    assert "selectedLocationsFacet=300000000361862" in finder(route.calls[0])  # cached, no second lookup


@respx.mock
async def test_unknown_country_is_a_config_error_not_zero_jobs():
    respx.get(LIST).mock(return_value=page([], facets=[{"Id": 1, "Name": "India"}]))
    with pytest.raises(SourceError, match="United States"):
        await collect(OracleSource("TI", HOST, "CX"))


@respx.mock
async def test_enrich_takes_the_description_and_the_exact_posting_time():
    route = respx.get(DETAIL).mock(return_value=httpx.Response(200, json={"items": [{
        "ExternalDescriptionStr": "<p>Build compilers.</p>",
        "ExternalResponsibilitiesStr": "<ul><li>Own the backend</li></ul>",
        "ExternalQualificationsStr": "Minimum qualifications: 2+ years",
        "CorporateDescriptionStr": "About TI boilerplate",
        "ExternalPostedStartDate": "2026-10-13T16:18:21+00:00",
    }]}))
    job = Job(company="TI", source_id="25018416", title="SWE", url="u")
    async with httpx.AsyncClient() as client:
        await OracleSource("TI", HOST, "CX").enrich(client, job)

    assert finder(route.calls[0]) == 'ById;Id="25018416",siteNumber=CX'
    assert job.description == "Build compilers.\nOwn the backend\nMinimum qualifications: 2+ years"
    assert job.posted_at.isoformat() == "2026-10-13T16:18:21+00:00"


def test_registered_as_a_config_type():
    src = build_source({"company": "JPMorgan Chase", "type": "oracle", "host": "jpmc.fa.oraclecloud.com",
                        "site": "CX_1001"})
    assert isinstance(src, OracleSource) and src.country == "United States"
