from __future__ import annotations

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
                        click: str | None = None) -> None: ...


def _line(a: Alert) -> str:
    loc = "; ".join(a.job.locations[:2]) or "location n/a"
    labels = f" [{', '.join(a.labels)}]" if a.labels else ""
    return f"{a.job.company}: {a.job.title} ({loc}){labels}"


class ConsoleNotifier:
    async def send(self, alerts: list[Alert]) -> None:
        for a in alerts:
            print(f"[ALERT p{int(a.priority)}] {_line(a)}\n    {a.job.url}", flush=True)

    async def send_text(self, title: str, message: str, priority: Priority = Priority.DEFAULT,
                        click: str | None = None) -> None:
        print(f"[NOTICE p{int(priority)}] {title}: {message}" + (f"\n    {click}" if click else ""), flush=True)


class NtfyNotifier:
    def __init__(self, client: httpx.AsyncClient, server: str, topic: str, token: str | None = None):
        self.client, self.url, self.topic = client, server.rstrip("/"), topic
        self.headers = {"Authorization": f"Bearer {token}"} if token else {}

    async def send(self, alerts: list[Alert]) -> None:
        if len(alerts) <= DIGEST_THRESHOLD:
            for a in alerts:
                await self._publish(
                    title=f"{a.job.company}: {a.job.title}",
                    message="\n".join(filter(None, ["; ".join(a.job.locations[:3]), ", ".join(a.labels)])),
                    priority=a.priority,
                    click=a.job.url,
                    tags=["repeat"] if "repost" in a.labels else ["briefcase"],
                )
            return
        # Digest: one push per priority so reposts stay quiet.
        for prio in sorted({a.priority for a in alerts}, reverse=True):
            group = [a for a in alerts if a.priority == prio]
            await self._publish(
                title=f"{len(group)} new posting{'s' if len(group) != 1 else ''}",
                message="\n".join(f"{_line(a)}\n{a.job.url}" for a in group),
                priority=prio,
                click=None,
                tags=["briefcase"],
            )

    async def send_text(self, title: str, message: str, priority: Priority = Priority.DEFAULT,
                        click: str | None = None) -> None:
        await self._publish(title=title, message=message, priority=priority, click=click,
                            tags=["page_facing_up"] if click else ["warning"])

    async def _publish(self, title: str, message: str, priority: Priority, click: str | None, tags: list[str]) -> None:
        # JSON publishing avoids header-encoding problems with non-ASCII titles.
        payload = {"topic": self.topic, "title": title, "message": message or title, "priority": int(priority),
                   "tags": tags}
        if click:
            # Tap target alone is invisible once the notification is gone; the text copy and
            # button keep the link usable from history and the web app.
            payload["message"] = f"{message}\n{click}" if message else click
            payload["click"] = click
            payload["actions"] = [{"action": "view", "label": "Open posting", "url": click, "clear": False}]
        resp = await self.client.post(self.url, json=payload, headers=self.headers)
        resp.raise_for_status()
