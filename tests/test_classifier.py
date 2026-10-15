import asyncio
import json
import time

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
async def test_burst_batches_run_in_parallel_up_to_the_limit(db, cfg):
    cfg.max_parallel_batches = 2
    in_flight, peak = 0, 0

    async def slow(request):
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        await asyncio.sleep(0.05)
        in_flight -= 1
        n = len(json.loads(request.content)["contents"][0]["parts"][0]["text"].split("=== id:")) - 1
        return gemini_reply([item(str(i)) for i in range(n)])

    route = respx.post(URL).mock(side_effect=slow)
    jobs = [make_job(source_id=str(i)) for i in range(6)]  # 3 batches of 2
    async with httpx.AsyncClient() as client:
        out = await GeminiClassifier(cfg, db, client).classify(jobs)
    assert route.call_count == 3 and peak == 2
    assert set(out) == {f"Acme:{i}" for i in range(6)}


@respx.mock
async def test_parallel_batches_still_respect_the_daily_budget(db, cfg):
    cfg.max_parallel_batches, cfg.daily_limit = 3, 2
    route = respx.post(URL).mock(return_value=gemini_reply([item("0"), item("1")]))
    async with httpx.AsyncClient() as client:
        await GeminiClassifier(cfg, db, client).classify([make_job(source_id=str(i)) for i in range(6)])
    assert route.call_count == 2


@respx.mock
async def test_parallel_batch_starts_are_spaced_by_the_rate_limit(db, cfg):
    cfg.max_parallel_batches, cfg.requests_per_minute = 3, 600  # one start per 0.1s
    starts = []

    def record(request):
        starts.append(time.monotonic())
        return gemini_reply([item("0"), item("1")])

    respx.post(URL).mock(side_effect=record)
    async with httpx.AsyncClient() as client:
        await GeminiClassifier(cfg, db, client).classify([make_job(source_id=str(i)) for i in range(6)])
    gaps = [b - a for a, b in zip(starts, starts[1:], strict=False)]
    assert len(starts) == 3 and all(g >= 0.09 for g in gaps)  # queued for their slot, not all fired at once


@respx.mock
async def test_api_error_fails_open_and_stops_batching(db, cfg, monkeypatch):
    monkeypatch.setattr("jobwatch.classifier.RETRY_DELAYS", (0,))
    cfg.max_parallel_batches = 1  # batches already in flight finish; ones not yet started are skipped
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


CLAUDE_URL = "https://api.anthropic.com/v1/messages"


def claude_reply(items):
    # Haiku 5.5 puts an (empty) thinking block before the answer.
    return httpx.Response(200, json={"content": [
        {"type": "thinking", "thinking": "", "signature": "x"},
        {"type": "text", "text": json.dumps({"postings": items})},
    ]})


@respx.mock
async def test_claude_is_the_last_resort_after_both_gemini_models(db, cfg, monkeypatch):
    monkeypatch.setattr("jobwatch.classifier.RETRY_DELAYS", (0,))
    cfg.fallback_model, cfg.fallback_requests_per_minute = "backup-model", 6000
    cfg.claude_api_key, cfg.claude_requests_per_minute = "ck", 6000
    respx.post(URL).mock(return_value=httpx.Response(503))
    respx.post(FALLBACK_URL).mock(return_value=httpx.Response(500))
    claude = respx.post(CLAUDE_URL).mock(return_value=claude_reply([item("0", level="entry")]))
    async with httpx.AsyncClient() as client:
        out = await GeminiClassifier(cfg, db, client).classify([make_job(source_id="0", description="desc")])

    assert out["Acme:0"]["level"] == "entry"
    req = claude.calls[0].request
    assert req.headers["x-api-key"] == "ck" and req.headers["anthropic-version"] == "2023-06-01"
    body = json.loads(req.content)
    assert body["model"] == "claude-haiku-5-5"
    # Structured output, not a forced tool call: Sonnet 5.5 rejects tool_choice "tool".
    assert "tools" not in body and "tool_choice" not in body
    schema = body["output_config"]["format"]["schema"]
    items = schema["properties"]["postings"]["items"]
    assert items["properties"]["min_years"]["type"] == ["integer", "null"]  # Gemini's nullable, in JSON Schema
    assert items["properties"]["level"]["enum"][0] == "intern"
    assert schema["additionalProperties"] is False and items["additionalProperties"] is False
    assert "desc" in body["messages"][0]["content"]


@respx.mock
async def test_claude_alone_works_without_a_gemini_key(db, monkeypatch):
    cfg = ClassifierConfig(api_key=None, claude_api_key="ck", claude_requests_per_minute=6000)
    gemini = respx.post(url__startswith="https://generativelanguage.googleapis.com").mock(return_value=gemini_reply([]))
    respx.post(CLAUDE_URL).mock(return_value=claude_reply([item("0")]))
    async with httpx.AsyncClient() as client:
        out = await GeminiClassifier(cfg, db, client).classify([make_job(source_id="0")])
    assert gemini.call_count == 0 and "Acme:0" in out


@respx.mock
async def test_claude_reply_that_is_not_the_schema_is_unusable(db, monkeypatch):
    cfg = ClassifierConfig(api_key=None, claude_api_key="ck", claude_requests_per_minute=6000)
    respx.post(CLAUDE_URL).mock(return_value=httpx.Response(200, json={"content": [{"type": "text", "text": "no"}]}))
    async with httpx.AsyncClient() as client:
        assert await GeminiClassifier(cfg, db, client).classify([make_job(source_id="0")]) == {}


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
