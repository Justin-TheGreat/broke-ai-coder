from __future__ import annotations

from datetime import timedelta

import pytest
from conftest import NOW

from app.db.retention import run_retention_cleanup
from app.timeutil import to_db

OLD = to_db(NOW - timedelta(days=90))
RECENT = to_db(NOW - timedelta(days=5))
CUTOFF = NOW - timedelta(days=60)


def task(db, tid, status="SUCCEEDED", created=OLD, finished=OLD, session=None):
    db.execute(
        "INSERT INTO tasks(id, project_id, session_id, discord_user_id, prompt, status,"
        " created_at, finished_at, last_event_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (tid, "p", session, "u", "x", status, created, finished, created),
    )


def session(db, sid, status="CLOSED", closed=OLD):
    db.execute(
        "INSERT INTO sessions(id, project_id, status, created_at, updated_at, closed_at)"
        " VALUES (?,?,?,?,?,?)",
        (sid, "p", status, OLD, OLD, closed if status == "CLOSED" else None),
    )


def usage(db, tid, at):
    db.execute(
        "INSERT INTO usage_events(task_id, provider, model, cost_class, status, occurred_at)"
        " VALUES (?,?,?,?,?,?)",
        (tid, "a", "m", "FREE", "ok", at),
    )


def attempt(db, tid, n, at):
    db.execute(
        "INSERT INTO fallback_attempts(task_id, attempt_no, provider, model, status, started_at)"
        " VALUES (?,?,?,?,?,?)",
        (tid, n, "a", "m", "failed", at),
    )


def pevent(db, tid, at):
    db.execute(
        "INSERT INTO provider_events(provider, task_id, event_type, created_at) VALUES (?,?,?,?)",
        ("a", tid, "e", at),
    )


def quota(db, at):
    db.execute(
        "INSERT INTO quota_snapshots(provider, quota_window, unit, confidence, observed_at,"
        " source) VALUES ('a','day','REQUESTS','EXACT',?,'t')",
        (at,),
    )


def count(db, table):
    return db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def ids(db, table):
    return {r[0] for r in db.execute(f"SELECT id FROM {table}")}


def seed(db):
    task(db, "old")
    usage(db, "old", OLD)
    attempt(db, "old", 1, OLD)
    pevent(db, "old", OLD)
    pevent(db, None, OLD)
    usage(db, None, OLD)
    quota(db, OLD)
    task(db, "recent", created=RECENT, finished=RECENT)
    usage(db, "recent", RECENT)
    quota(db, RECENT)
    task(db, "running", status="RUNNING", finished=None)
    usage(db, "running", OLD)
    attempt(db, "running", 1, OLD)
    session(db, "open", status="OPEN")
    task(db, "in_open", session="open")
    session(db, "closed_empty")
    session(db, "closed_prot")
    task(db, "prot", status="RUNNING", finished=None, session="closed_prot")


def test_cleanup_full(db):
    seed(db)
    res = run_retention_cleanup(db, now=NOW)
    assert res.cutoff == CUTOFF
    assert ids(db, "tasks") == {"recent", "running", "in_open", "prot"}
    assert ids(db, "sessions") == {"open", "closed_prot"}
    assert count(db, "fallback_attempts") == 1
    assert db.execute("SELECT task_id FROM fallback_attempts").fetchone()[0] == "running"
    tids = sorted(r[0] or "" for r in db.execute("SELECT task_id FROM usage_events"))
    assert tids == ["recent", "running"]
    assert count(db, "provider_events") == 0
    assert count(db, "quota_snapshots") == 1
    assert res.db_size_bytes > 0
    assert res.deleted["tasks"] == 1
    assert res.deleted["sessions"] == 1


def test_idempotent(db):
    seed(db)
    run_retention_cleanup(db, now=NOW)
    res = run_retention_cleanup(db, now=NOW)
    assert all(v == 0 for v in res.deleted.values())


def test_batch_size_one(db):
    for i in range(5):
        task(db, f"o{i}")
        usage(db, f"o{i}", OLD)
        quota(db, OLD)
    res = run_retention_cleanup(db, now=NOW, batch_size=1)
    assert count(db, "tasks") == 0
    assert count(db, "usage_events") == 0
    assert count(db, "quota_snapshots") == 0
    assert res.deleted["tasks"] == 5


def test_cutoff_boundary_strict(db):
    at = to_db(CUTOFF)
    before = to_db(CUTOFF - timedelta(microseconds=1))
    task(db, "edge", created=at, finished=at)
    task(db, "before", created=before, finished=before)
    quota(db, at)
    quota(db, before)
    run_retention_cleanup(db, now=NOW)
    assert ids(db, "tasks") == {"edge"}
    assert count(db, "quota_snapshots") == 1


def test_provider_events_set_null_when_task_deleted(db):
    task(db, "old")
    pevent(db, "old", RECENT)
    run_retention_cleanup(db, now=NOW)
    assert count(db, "tasks") == 0
    assert db.execute("SELECT task_id FROM provider_events").fetchone()[0] is None


def test_cascade_children_of_deleted_task(db):
    task(db, "old")
    attempt(db, "old", 1, RECENT)
    run_retention_cleanup(db, now=NOW)
    assert count(db, "fallback_attempts") == 0


def test_invalid_args(db):
    with pytest.raises(ValueError):
        run_retention_cleanup(db, now=NOW, retention_days=0)
    with pytest.raises(ValueError):
        run_retention_cleanup(db, now=NOW, batch_size=0)


def test_custom_retention_days(db):
    at = to_db(NOW - timedelta(days=10))
    task(db, "t", created=at, finished=at)
    run_retention_cleanup(db, now=NOW, retention_days=7)
    assert count(db, "tasks") == 0
