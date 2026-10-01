from datetime import timedelta

from conftest import NOW, make_job

from jobwatch.repost import RepostRules, repost_reason

RULES = RepostRules()
DESC = " ".join(f"word{i}" for i in range(300))


def test_fresh_posting_is_not_a_repost(db):
    job = make_job(posted_at=NOW, created_at=NOW - timedelta(days=2), req_id="R1", description=DESC)
    assert repost_reason(job, db, NOW, RULES) is None


def test_old_requisition_reposted(db):
    job = make_job(posted_at=NOW, created_at=NOW - timedelta(days=57))
    assert "57d" in repost_reason(job, db, NOW, RULES)


def test_new_to_us_but_published_long_ago(db):
    job = make_job(posted_at=NOW - timedelta(days=30))
    assert repost_reason(job, db, NOW, RULES) == "first published 30d ago"


def test_recently_published_is_not_stale(db):
    assert repost_reason(make_job(posted_at=NOW - timedelta(days=3)), db, NOW, RULES) is None


def test_same_requisition_new_posting_id(db):
    db.insert(make_job(source_id="old", req_id="R1"), "notified", NOW - timedelta(days=30))
    job = make_job(source_id="new", req_id="R1")
    assert repost_reason(job, db, NOW, RULES) == "same requisition as an earlier posting"


def test_requisition_in_other_company_does_not_count(db):
    db.insert(make_job(source_id="old", req_id="R1", company="Other"), "notified", NOW)
    assert repost_reason(make_job(source_id="new", req_id="R1"), db, NOW, RULES) is None


def test_text_mention(db):
    job = make_job(description="Note: this is a re-posting. If you previously applied, no need to reapply.")
    assert "re-posting" in repost_reason(job, db, NOW, RULES)


def test_near_identical_description_same_title(db):
    db.insert(make_job(source_id="old", description=DESC), "filtered", NOW - timedelta(days=20))
    job = make_job(source_id="new", description=DESC + " Updated pay range.")
    assert repost_reason(job, db, NOW, RULES) == "near-identical to a posting seen 20d ago"


def test_similar_description_outside_lookback_ignored(db):
    db.insert(make_job(source_id="old", description=DESC), "filtered", NOW - timedelta(days=200))
    assert repost_reason(make_job(source_id="new", description=DESC), db, NOW, RULES) is None


def test_different_description_same_title_is_new(db):
    db.insert(make_job(source_id="old", description=DESC), "notified", NOW - timedelta(days=5))
    other = " ".join(f"other{i}" for i in range(300))
    assert repost_reason(make_job(source_id="new", description=other), db, NOW, RULES) is None
