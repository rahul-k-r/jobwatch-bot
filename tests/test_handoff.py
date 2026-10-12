import json
from datetime import UTC, datetime, timedelta

import pytest
from conftest import make_job

from jobwatch.handoff import Handoff, age_from


def test_queue_writes_full_job_atomically(tmp_path):
    h = Handoff(tmp_path)
    job = make_job("1", "Forward Deployed Engineer", description="JD text", canaries=["banana"])
    path = h.queue(job)
    assert path.parent == tmp_path / "inbox"
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["key"] == "Acme:1" and data["description"] == "JD text" and data["canaries"] == ["banana"]
    assert "queued_at" in data
    assert not list((tmp_path / "inbox").glob("*.tmp"))


D = datetime(2026, 10, 12, tzinfo=UTC)  # a date-only posted_at: midnight UTC


@pytest.mark.parametrize("posted_at, prev_poll_at, expected", [
    # Real timestamp: that is the age basis.
    (D + timedelta(hours=15, minutes=7), D + timedelta(hours=16), D + timedelta(hours=15, minutes=7)),
    # Date only, seen by a poll 5 min after the previous one: it appeared after that previous poll.
    (D, D + timedelta(hours=16), D + timedelta(hours=16)),
    # Date only, no earlier poll known (first run, or the poller was off): only the date is known.
    (D, None, D),
    # Date only, but the date itself is days old (e.g. surfaced late behind newer postings).
    (D - timedelta(days=5), D + timedelta(hours=16), D - timedelta(days=5) + timedelta(hours=32)),
    (None, D, None),
])
def test_age_from(posted_at, prev_poll_at, expected):
    assert age_from(make_job(posted_at=posted_at, prev_poll_at=prev_poll_at)) == expected


def test_queue_includes_age_basis(tmp_path):
    job = make_job(posted_at=D, prev_poll_at=D + timedelta(hours=16))
    data = json.loads(Handoff(tmp_path).queue(job).read_text(encoding="utf-8"))
    assert data["age_from"] == "2026-10-12T16:00:00+00:00"
    assert data["prev_poll_at"] == "2026-10-12T16:00:00+00:00"


def test_queue_file_name_is_filesystem_safe(tmp_path):
    path = Handoff(tmp_path).queue(make_job("a/b:c?*", "t", company="Weights & Biases (CoreWeave)"))
    assert all(ch not in path.name for ch in '/\\:?*&() ')


def test_collect_and_mark_sent(tmp_path):
    h = Handoff(tmp_path)
    (tmp_path / "outbox" / "x.json").write_text(json.dumps({"key": "Acme:1", "status": "ready"}), encoding="utf-8")
    (tmp_path / "outbox" / "partial.json.tmp").write_text("{", encoding="utf-8")
    [(path, result)] = h.collect()
    assert result["status"] == "ready"
    h.mark_sent(path)
    assert h.collect() == []
    assert (tmp_path / "outbox" / "sent" / "x.json").exists()


def test_unreadable_outbox_file_is_skipped(tmp_path):
    h = Handoff(tmp_path)
    (tmp_path / "outbox" / "bad.json").write_text("not json", encoding="utf-8")
    assert h.collect() == []
