"""Greenhouse, Ashby, Lever and Workable adapters (the company job-board platforms)."""

import httpx
import pytest
import respx
from conftest import load_fixture

from jobwatch.models import Job
from jobwatch.sources.ashby import AshbySource
from jobwatch.sources.base import SourceError
from jobwatch.sources.greenhouse import GreenhouseSource
from jobwatch.sources.lever import LeverSource
from jobwatch.sources.workable import WorkableSource

GH = "https://boards-api.greenhouse.io/v1/boards/anthropic"


async def collect(src):
    async with httpx.AsyncClient() as client:
        return [page async for page in src.pages(client)]


@respx.mock
async def test_greenhouse_list_is_light_and_detail_fills_offices():
    route = respx.get(f"{GH}/jobs").mock(return_value=httpx.Response(200, json=load_fixture("greenhouse_jobs.json")))
    respx.get(f"{GH}/jobs/5391151008").mock(return_value=httpx.Response(200, json=load_fixture("greenhouse_job.json")))
    src = GreenhouseSource("Anthropic", "anthropic")
    [page] = await collect(src)

    assert "content" not in route.calls[0].request.url.params  # no multi-MB content=true polling
    job = page[0]
    assert job.key == "Anthropic:5391151008"
    assert job.req_id == "4532973008"
    assert job.url == "https://job-boards.greenhouse.io/anthropic/jobs/5391151008"
    assert job.locations == []  # unknown until the detail call
    assert job.posted_at.date().isoformat() == "2026-08-14"
    assert page[1].title == "Applied AI Engineer"

    async with httpx.AsyncClient() as client:
        await src.enrich(client, job)
    assert job.locations == ["San Francisco, California, United States"]
    assert "3+ years of experience in software engineering" in job.description
    assert "&lt;" not in job.description and "<" not in job.description


@respx.mock
async def test_greenhouse_empty_board_yields_nothing_and_bad_payload_raises():
    respx.get(f"{GH}/jobs").mock(side_effect=[
        httpx.Response(200, json={"jobs": [], "meta": {"total": 0}}),
        httpx.Response(200, json={"error": "nope"}),
    ])
    src = GreenhouseSource("Anthropic", "anthropic")
    assert await collect(src) == []
    with pytest.raises(SourceError):
        await collect(src)


@respx.mock
async def test_ashby_structured_country_and_filters():
    respx.get("https://api.ashbyhq.com/posting-api/job-board/openai").mock(
        return_value=httpx.Response(200, json=load_fixture("ashby_board.json")))
    [page] = await collect(AshbySource("OpenAI", "openai"))

    assert [j.source_id for j in page] == ["3c67f712-697d-48d8-b05c-01be896e61da",
                                           "2560ed50-5535-42b8-b069-9ebc28ce7493"]  # intern + unlisted dropped
    swe, research = page
    assert swe.locations == ["San Francisco, United States"]
    assert swe.description.startswith("ABOUT THE TEAM")
    assert research.locations == ["San Francisco, United States", "London, UK, United Kingdom"]
    assert research.description == "The Safety Systems team"  # falls back to HTML
    assert research.posted_at.year == 2023


@respx.mock
async def test_lever_us_country_and_description_sections():
    route = respx.get("https://api.lever.co/v0/postings/palantir").mock(
        return_value=httpx.Response(200, json=load_fixture("lever_postings.json")))
    [page] = await collect(LeverSource("Palantir", "palantir"))

    assert route.calls[0].request.url.params["mode"] == "json"
    assert [j.title for j in page] == ["Backend Software Engineer - Application Development",
                                       "Forward Deployed Software Engineer"]  # internship dropped
    us, sg = page
    assert us.locations == ["New York, NY, United States", "Washington, D.C., United States"]
    assert sg.locations == ["Singapore, Singapore"]
    assert "What We Require\n2+ years of experience with Java or Go" in us.description
    assert "$135,000 - $200,000" in us.description
    assert us.posted_at.year == 2024


def test_lever_eu_region_host():
    assert LeverSource("Qonto", "qonto", region="eu").url == "https://api.eu.lever.co/v0/postings/qonto"


@respx.mock
async def test_workable_locations_and_description():
    route = respx.get("https://apply.workable.com/api/v1/widget/accounts/huggingface").mock(
        return_value=httpx.Response(200, json=load_fixture("workable_account.json")))
    [page] = await collect(WorkableSource("Hugging Face", "huggingface"))

    assert route.calls[0].request.url.params["details"] == "true"
    paris, us = page
    assert paris.key == "Hugging Face:F4C096B22E"
    assert paris.locations == ["Paris, Île-de-France, France"]
    assert us.locations == ["United States"]  # falls back to top-level fields
    assert us.posted_at.date().isoformat() == "2026-09-20"
    assert paris.description == "At Hugging Face, we're on a journey to democratize good AI."


@respx.mock
async def test_non_list_lever_payload_raises():
    respx.get("https://api.lever.co/v0/postings/nope").mock(
        return_value=httpx.Response(200, json={"ok": False, "error": "Document not found"}))
    with pytest.raises(SourceError):
        await collect(LeverSource("Nope", "nope"))


def test_job_roundtrip_keeps_fields():
    job = Job(company="A", source_id="1", title="t", url="u", locations=["x"])
    assert Job.from_dict(job.to_dict()) == job
