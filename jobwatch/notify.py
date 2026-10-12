from __future__ import annotations

import json
import logging
from typing import Protocol

import httpx

from jobwatch.models import Alert, Priority

log = logging.getLogger(__name__)

# Above this many alerts in one cycle, send a single digest instead of one push per job
# (ntfy.sh allows a burst of 60 then 1 request / 5s per IP).
DIGEST_THRESHOLD = 5


class Notifier(Protocol):
    async def send(self, alerts: list[Alert]) -> None:
        """Deliver alerts; raise on failure so the jobs stay pending and retry next cycle."""

    async def send_text(self, title: str, message: str, priority: Priority = Priority.DEFAULT,
                        click: str | None = None, attachment: tuple[str, bytes] | None = None,
                        channel: str | None = None) -> None:
        """channel="resumes" for build results; None for operational notices."""


def _line(a: Alert) -> str:
    loc = "; ".join(a.job.locations[:2]) or "location n/a"
    labels = f" [{', '.join(a.labels)}]" if a.labels else ""
    return f"{a.job.company}: {a.job.title} ({loc}){labels}"


class ConsoleNotifier:
    async def send(self, alerts: list[Alert]) -> None:
        for a in alerts:
            print(f"[ALERT p{int(a.priority)}] {_line(a)}\n    {a.job.url}", flush=True)

    async def send_text(self, title: str, message: str, priority: Priority = Priority.DEFAULT,
                        click: str | None = None, attachment: tuple[str, bytes] | None = None,
                        channel: str | None = None) -> None:
        print(f"[NOTICE p{int(priority)}] {title}: {message}" + (f"\n    {click}" if click else ""), flush=True)


class NtfyNotifier:
    """Three topics so each can be muted or prioritised separately on the phone: new postings (and
    operational notices), built resumes, and low-priority alerts such as reposts. Unset ones fall
    back to the main topic."""

    def __init__(self, client: httpx.AsyncClient, server: str, topic: str, token: str | None = None,
                 resume_topic: str | None = None, low_topic: str | None = None):
        self.client, self.url, self.topic = client, server.rstrip("/"), topic
        self.resume_topic, self.low_topic = resume_topic or topic, low_topic or topic
        self.headers = {"Authorization": f"Bearer {token}"} if token else {}

    def _alert_topic(self, priority: Priority) -> str:
        return self.low_topic if priority < Priority.DEFAULT else self.topic

    async def send(self, alerts: list[Alert]) -> None:
        if len(alerts) <= DIGEST_THRESHOLD:
            for a in alerts:
                await self._publish(
                    topic=self._alert_topic(a.priority),
                    title=f"{a.job.company}: {a.job.title}",
                    message="\n".join(filter(None, ["; ".join(a.job.locations[:3]),
                                                    (a.job.classification or {}).get("highlights"),
                                                    ", ".join(a.labels)])),
                    priority=a.priority,
                    click=a.job.url,
                    tags=["repeat"] if "repost" in a.labels else ["briefcase"],
                )
            return
        # Digest: one push per priority so reposts stay quiet.
        for prio in sorted({a.priority for a in alerts}, reverse=True):
            group = [a for a in alerts if a.priority == prio]
            await self._publish(
                topic=self._alert_topic(prio),
                title=f"{len(group)} new posting{'s' if len(group) != 1 else ''}",
                message="\n".join(f"{_line(a)}\n{a.job.url}" for a in group),
                priority=prio,
                click=None,
                tags=["briefcase"],
            )

    async def send_text(self, title: str, message: str, priority: Priority = Priority.DEFAULT,
                        click: str | None = None, attachment: tuple[str, bytes] | None = None,
                        channel: str | None = None) -> None:
        topic = self.resume_topic if channel == "resumes" else self.topic
        tags = ["page_facing_up"] if click else ["warning"]
        if attachment:
            try:
                await self._publish(topic, title, message, priority, click, tags, attachment)
                return
            except httpx.HTTPStatusError as e:
                # Size/quota rejections won't succeed on retry; still deliver the notice. 429 and 5xx
                # propagate so the result is retried with the PDF.
                if e.response.status_code == 429 or e.response.status_code >= 500:
                    raise
                log.warning("ntfy rejected the attachment (%d); sending without it", e.response.status_code)
                message = f"{message}\n(PDF not attached: ntfy returned {e.response.status_code})"
        await self._publish(topic, title, message, priority, click, tags)

    async def _publish(self, topic: str, title: str, message: str, priority: Priority, click: str | None,
                       tags: list[str], attachment: tuple[str, bytes] | None = None) -> None:
        payload = {"topic": topic, "title": title, "message": message or title, "priority": int(priority),
                   "tags": tags}
        if click:
            # Tap target alone is invisible once the notification is gone; the text copy keeps the
            # link usable from history and the web app.
            payload["message"] = f"{message}\n{click}" if message else click
            payload["click"] = click
        if attachment:
            # A file upload is the request body, so the fields go in query params; headers would
            # break on non-ASCII titles.
            filename, data = attachment
            params = {k: v if isinstance(v, str) else json.dumps(v) for k, v in payload.items() if k != "topic"}
            params["tags"], params["priority"] = ",".join(tags), str(int(priority))
            resp = await self.client.put(f"{self.url}/{topic}", content=data,
                                         params={**params, "filename": filename}, headers=self.headers)
        else:
            # JSON publishing avoids header-encoding problems with non-ASCII titles.
            resp = await self.client.post(self.url, json=payload, headers=self.headers)
        resp.raise_for_status()
