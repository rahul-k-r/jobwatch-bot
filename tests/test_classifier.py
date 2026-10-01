import json

import httpx
import pytest
import respx
from conftest import make_job

from jobwatch.classifier import ClassifierConfig, GeminiClassifier, _excerpt, exclusion_reason

URL = "https://generativelanguage.googleapis.com/v1beta/models/test-model:generateContent"


def gemini_reply(items):
    return httpx.Response(200, json={"candidates": [{"content": {"parts": [{"text": json.dumps(items)}]}}]})


def item(id_, **kw):
    return {"id": id_, "level": "mid", "min_years": 2, "mentions_repost": False, "sponsorship": "unspecified",
            "phd_required": False, "reason": "r", **kw}


@pytest.fixture
def cfg():
    return ClassifierConfig(api_key="k", model="test-model", requests_per_minute=6000, batch_size=2, daily_limit=10)


@respx.mock
async def test_batches_and_maps_results_back_to_jobs(db, cfg):
    route = respx.post(URL).mock(side_effect=[
        gemini_reply([item("0"), item("1", level="senior", min_years=8)]),
        gemini_reply([item("0", mentions_repost=True)]),
    ])
    jobs = [make_job(source_id=str(i), description="desc") for i in range(3)]
    async with httpx.AsyncClient() as client:
        out = await GeminiClassifier(cfg, db, client).classify(jobs)

    assert route.call_count == 2
    assert out["Acme:1"]["min_years"] == 8
    assert out["Acme:2"]["mentions_repost"] is True
    assert route.calls[0].request.headers["x-goog-api-key"] == "k"


@respx.mock
async def test_transient_server_error_is_retried(db, cfg, monkeypatch):
    # Seen live: a 503 on the first batch left a whole cycle's alerts unclassified.
    monkeypatch.setattr("jobwatch.classifier.RETRY_DELAYS", (0,))
    route = respx.post(URL).mock(side_effect=[httpx.Response(503), gemini_reply([item("0")])])
    async with httpx.AsyncClient() as client:
        out = await GeminiClassifier(cfg, db, client).classify([make_job(source_id="0")])
    assert route.call_count == 2
    assert out["Acme:0"]["level"] == "mid"


@respx.mock
async def test_api_error_fails_open_and_stops_batching(db, cfg, monkeypatch):
    monkeypatch.setattr("jobwatch.classifier.RETRY_DELAYS", (0,))
    route = respx.post(URL).mock(return_value=httpx.Response(400))
    jobs = [make_job(source_id=str(i)) for i in range(4)]
    async with httpx.AsyncClient() as client:
        assert await GeminiClassifier(cfg, db, client).classify(jobs) == {}
    assert route.call_count == 1


FALLBACK_URL = "https://generativelanguage.googleapis.com/v1beta/models/backup-model:generateContent"


@respx.mock
async def test_fallback_model_used_when_primary_keeps_failing(db, cfg, monkeypatch):
    monkeypatch.setattr("jobwatch.classifier.RETRY_DELAYS", (0,))
    cfg.fallback_model, cfg.fallback_requests_per_minute = "backup-model", 6000
    primary = respx.post(URL).mock(return_value=httpx.Response(503))
    backup = respx.post(FALLBACK_URL).mock(return_value=gemini_reply([item("0")]))
    async with httpx.AsyncClient() as client:
        out = await GeminiClassifier(cfg, db, client).classify([make_job(source_id="0")])
    assert primary.call_count == 2 and backup.call_count == 1
    assert out["Acme:0"]["level"] == "mid"
    assert backup.calls[0].request.extensions["timeout"]["read"] == cfg.timeout


@respx.mock
async def test_fallback_used_when_primary_budget_is_spent(db, cfg):
    cfg.daily_limit = 0
    cfg.fallback_model, cfg.fallback_requests_per_minute = "backup-model", 6000
    primary = respx.post(URL).mock(return_value=gemini_reply([item("0")]))
    backup = respx.post(FALLBACK_URL).mock(return_value=gemini_reply([item("0")]))
    async with httpx.AsyncClient() as client:
        out = await GeminiClassifier(cfg, db, client).classify([make_job(source_id="0")])
    assert primary.call_count == 0 and backup.call_count == 1
    assert "Acme:0" in out


@respx.mock
async def test_daily_budget_is_enforced(db, cfg):
    cfg.daily_limit = 1
    route = respx.post(URL).mock(return_value=gemini_reply([item("0"), item("1")]))
    jobs = [make_job(source_id=str(i)) for i in range(4)]
    async with httpx.AsyncClient() as client:
        out = await GeminiClassifier(cfg, db, client).classify(jobs)
    assert route.call_count == 1
    assert set(out) == {"Acme:0", "Acme:1"}


@pytest.mark.parametrize("c, excluded", [
    (item("0", min_years=2), False),
    (item("0", min_years=5), True),
    (item("0", min_years=3, level="senior"), False),  # stated years beat the label (combo postings)
    (item("0", min_years=None, level="senior"), True),
    (item("0", min_years=None, level="unknown"), False),
    (item("0", sponsorship="not_available"), True),
    (item("0", phd_required=True), True),
    (item("0", phd_required=False), False),
    ({k: v for k, v in item("0").items() if k != "phd_required"}, False),  # older stored classifications
    # "Technical Sourcer, Research SWE": Gemini saw a recruiting role; that must drop it.
    (item("0", min_years=None, level="unknown", in_scope=False), True),
    ({k: v for k, v in item("0").items() if k != "in_scope"}, False),  # missing -> fail open
    # "Software Engineer: Intership Opportunities": the title regex misses the typo, Gemini doesn't.
    (item("0", min_years=None, level="intern"), True),
    (item("0", min_years=0, level="intern"), True),  # stated years don't rescue an internship
    (item("0", min_years=None, level="entry"), False),  # new grad stays in
])
def test_exclusion_reason(c, excluded):
    assert (exclusion_reason(c, exclude_min_years=5, require_sponsorship=True) is not None) is excluded


def test_schema_and_prompt_ask_for_phd_requirement():
    from jobwatch.classifier import _PROMPT, _SCHEMA
    assert "phd_required" in _SCHEMA["items"]["required"]
    assert "Master" in _PROMPT


def test_schema_and_prompt_ask_whether_the_role_is_engineering_at_all():
    from jobwatch.classifier import _PROMPT, _SCHEMA
    assert "in_scope" in _SCHEMA["items"]["required"]
    assert "recruiting" in _PROMPT and "even if the title" in _PROMPT


def test_schema_and_prompt_ask_for_role_highlights():
    from jobwatch.classifier import _PROMPT, _SCHEMA
    assert _SCHEMA["items"]["properties"]["highlights"]["type"] == "STRING"
    assert "highlights" in _SCHEMA["items"]["required"]
    assert "highlights:" in _PROMPT


def test_schema_and_prompt_separate_internships_from_new_grad():
    from jobwatch.classifier import _PROMPT, _SCHEMA
    assert "intern" in _SCHEMA["items"]["properties"]["level"]["enum"]
    assert "co-op" in _PROMPT and "new grad" in _PROMPT


def test_excerpt_keeps_qualifications_of_long_posting():
    text = "intro " * 2000 + "Required Qualifications: 3+ years of Python." + " benefits" * 500
    out = _excerpt(text, 2000)
    assert len(out) <= 2010
    assert "3+ years of Python" in out
