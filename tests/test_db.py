from datetime import UTC, datetime

from conftest import make_job

NOW = datetime(2026, 9, 30, tzinfo=UTC)


def test_uses_wal_so_commits_dont_recreate_a_journal_file(db):
    assert db.conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


def test_insert_many_writes_a_page_in_one_transaction(db):
    jobs = [make_job(source_id=str(i)) for i in range(3)]
    db.insert_many([jobs[0]], "seeded", NOW)
    db.insert_many(jobs, "seeded", NOW)  # already-known keys are ignored
    assert db.known_keys(j.key for j in jobs) == {j.key for j in jobs}
    assert not db.conn.in_transaction


def test_last_successful_poll_survives_a_restart(db, tmp_path):
    from jobwatch.db import DB

    assert db.last_polled("Acme") is None
    db.mark_polled("Acme", NOW)
    db.mark_polled("Acme", NOW.replace(hour=5))
    reopened = DB(str(tmp_path / "test.db"))
    try:
        assert reopened.last_polled("Acme") == NOW.replace(hour=5)
        assert reopened.last_polled("Other") is None
    finally:
        reopened.close()


def test_job_round_trips_prev_poll_at(db):
    db.insert(make_job(prev_poll_at=NOW), "pending", NOW)
    assert db.pending()[0].prev_poll_at == NOW
