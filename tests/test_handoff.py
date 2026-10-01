import json

from conftest import make_job

from jobwatch.handoff import Handoff


def test_queue_writes_full_job_atomically(tmp_path):
    h = Handoff(tmp_path)
    job = make_job("1", "Forward Deployed Engineer", description="JD text", canaries=["banana"])
    path = h.queue(job)
    assert path.parent == tmp_path / "inbox"
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["key"] == "Acme:1" and data["description"] == "JD text" and data["canaries"] == ["banana"]
    assert "queued_at" in data
    assert not list((tmp_path / "inbox").glob("*.tmp"))


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
