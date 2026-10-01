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
    # Link visible and copyable (web app, history), plus a button, not only the tap target.
    assert p["message"].endswith("\nhttps://x/1")
    assert p["actions"] == [{"action": "view", "label": "Open posting", "url": "https://x/1", "clear": False}]


@respx.mock
async def test_resume_ready_notice_links_the_posting():
    route = respx.post(SERVER).mock(return_value=httpx.Response(200))
    async with httpx.AsyncClient() as client:
        await NtfyNotifier(client, SERVER, "jobs").send_text("Resume ready: Acme - FDE", "a/cv.pdf",
                                                             Priority.HIGH, click="https://x/1")
    [p] = payloads(route)
    assert p["message"] == "a/cv.pdf\nhttps://x/1" and p["actions"][0]["url"] == "https://x/1"


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
