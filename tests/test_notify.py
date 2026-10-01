import json

import httpx
import pytest
import respx
from conftest import make_job

from jobwatch.models import Alert, Priority
from jobwatch.notify import NtfyNotifier

SERVER = "https://ntfy.example.com"


def payloads(route):
    return [json.loads(c.request.content) for c in route.calls]


@respx.mock
async def test_single_alert_payload_and_token():
    route = respx.post(SERVER).mock(return_value=httpx.Response(200))
    async with httpx.AsyncClient() as client:
        await NtfyNotifier(client, SERVER + "/", "jobs", token="tk_abc").send(
            [Alert(make_job("1", "Software Engineer II"), Priority.HIGH, ["2+ yrs"])]
        )
    [p] = payloads(route)
    assert p["topic"] == "jobs" and p["priority"] == 4
    assert p["title"] == "Acme: Software Engineer II"
    assert p["click"] == "https://x/1"
    assert "2+ yrs" in p["message"]
    assert route.calls[0].request.headers["Authorization"] == "Bearer tk_abc"
    # Link visible and copyable (web app, history), not only the tap target. No button: both
    # clients already render the link, and the web app showed two buttons doing the same thing.
    assert p["message"].endswith("\nhttps://x/1")
    assert "actions" not in p


@respx.mock
async def test_alert_shows_role_highlights_between_location_and_labels():
    route = respx.post(SERVER).mock(return_value=httpx.Response(200))
    job = make_job("1", "Software Engineer II")
    job.classification = {"highlights": "Go + Kubernetes, payments infra; hybrid 3 days"}
    async with httpx.AsyncClient() as client:
        await NtfyNotifier(client, SERVER, "jobs").send([Alert(job, Priority.HIGH, ["2+ yrs"])])
    [p] = payloads(route)
    lines = p["message"].split("\n")
    assert lines[1] == "Go + Kubernetes, payments infra; hybrid 3 days" and lines[2] == "2+ yrs"


@respx.mock
async def test_alert_without_highlights_is_unchanged():
    route = respx.post(SERVER).mock(return_value=httpx.Response(200))
    async with httpx.AsyncClient() as client:
        await NtfyNotifier(client, SERVER, "jobs").send([Alert(make_job("1"), Priority.HIGH, ["2+ yrs"])])
    [p] = payloads(route)
    assert p["message"].count("\n") == 2  # location, labels, link


@respx.mock
async def test_resume_ready_notice_links_the_posting():
    route = respx.post(SERVER).mock(return_value=httpx.Response(200))
    async with httpx.AsyncClient() as client:
        await NtfyNotifier(client, SERVER, "jobs").send_text("Resume ready: Acme - FDE", "a/cv.pdf",
                                                             Priority.HIGH, click="https://x/1")
    [p] = payloads(route)
    assert p["message"] == "a/cv.pdf\nhttps://x/1" and p["click"] == "https://x/1" and "actions" not in p


@respx.mock
async def test_resume_ready_uploads_the_pdf_as_an_attachment():
    pdf = ("acme-fde-1a2b.pdf", b"%PDF-1.5 resume")
    route = respx.put(f"{SERVER}/jobs").mock(return_value=httpx.Response(200))
    async with httpx.AsyncClient() as client:
        await NtfyNotifier(client, SERVER, "jobs", token="tk_abc").send_text(
            "Resume ready: Acme – FDE", "a/cv.pdf", Priority.HIGH, click="https://x/1", attachment=pdf)
    [call] = route.calls
    req = call.request
    assert req.content == b"%PDF-1.5 resume"
    # Query params, not headers: titles can be non-ASCII.
    assert req.url.params["filename"] == "acme-fde-1a2b.pdf"
    assert req.url.params["title"] == "Resume ready: Acme – FDE"
    assert req.url.params["message"] == "a/cv.pdf\nhttps://x/1"
    assert req.url.params["click"] == "https://x/1" and req.url.params["priority"] == "4"
    assert "actions" not in req.url.params
    assert req.headers["Authorization"] == "Bearer tk_abc"


@respx.mock
async def test_rejected_attachment_falls_back_to_a_text_push():
    pdf = ("a.pdf", b"%PDF-1.5")
    respx.put(f"{SERVER}/jobs").mock(return_value=httpx.Response(413))
    text = respx.post(SERVER).mock(return_value=httpx.Response(200))
    async with httpx.AsyncClient() as client:
        await NtfyNotifier(client, SERVER, "jobs").send_text("Resume ready: Acme - FDE", "a/cv.pdf",
                                                             Priority.HIGH, attachment=pdf)
    [p] = payloads(text)
    assert p["message"].startswith("a/cv.pdf") and "not attached" in p["message"]


@respx.mock
async def test_attachment_rate_limit_raises_for_retry():
    pdf = ("a.pdf", b"%PDF-1.5")
    respx.put(f"{SERVER}/jobs").mock(return_value=httpx.Response(429))
    async with httpx.AsyncClient() as client:
        with pytest.raises(httpx.HTTPStatusError):
            await NtfyNotifier(client, SERVER, "jobs").send_text("t", "m", attachment=pdf)


@respx.mock
async def test_burst_becomes_one_digest_per_priority():
    route = respx.post(SERVER).mock(return_value=httpx.Response(200))
    alerts = [Alert(make_job(str(i)), Priority.HIGH) for i in range(6)]
    alerts.append(Alert(make_job("r"), Priority.LOW, ["repost: x"]))
    async with httpx.AsyncClient() as client:
        await NtfyNotifier(client, SERVER, "jobs").send(alerts)
    high, low = payloads(route)
    assert (high["priority"], high["title"]) == (4, "6 new postings")
    assert (low["priority"], low["title"]) == (2, "1 new posting")
    assert "Authorization" not in route.calls[0].request.headers


@respx.mock
async def test_server_error_raises_so_jobs_stay_pending():
    respx.post(SERVER).mock(return_value=httpx.Response(429))
    async with httpx.AsyncClient() as client:
        with pytest.raises(httpx.HTTPStatusError):
            await NtfyNotifier(client, SERVER, "jobs").send([Alert(make_job(), Priority.HIGH)])
