from __future__ import annotations

from datetime import timedelta

import pytest
from conftest import NOW

from app.db.attempts import AttemptStatus, FallbackAttemptRepository
from app.db.sessions import AmbiguousReference, SessionRepository, SessionStatus
from app.db.tasks import TaskRepository
from app.orchestrator.state_machine import TaskStatus
from app.providers.base import CostClass


def test_create_get(db):
    repo = SessionRepository(db)
    s = repo.create(project_id="default", now=NOW)
    assert s.status == SessionStatus.OPEN and s.opencode_session_id is None
    assert repo.get(s.id) == s
    assert repo.get("nope") is None


def test_find_by_ref(db):
    repo = SessionRepository(db)
    a = repo.create(project_id="p", now=NOW)
    assert repo.find_by_ref(a.id).id == a.id
    assert repo.find_by_ref(a.id[:8]).id == a.id
    assert repo.find_by_ref("ffffff") is None
    ts = "2026-01-01T00:00:00+00:00"
    for sid in ("abc111", "abc222"):
        db.execute("INSERT INTO sessions VALUES (?, 'x', 'p', 'OPEN', ?, ?, NULL)", (sid, ts, ts))
    with pytest.raises(AmbiguousReference):
        repo.find_by_ref("abc")
    assert repo.find_by_ref("abc1").id == "abc111"


def test_set_opencode_session_id_once(db):
    repo = SessionRepository(db)
    s = repo.create(project_id="p", now=NOW)
    assert repo.set_opencode_session_id(s.id, "ses_a", now=NOW).opencode_session_id == "ses_a"
    assert repo.set_opencode_session_id(s.id, "ses_b", now=NOW).opencode_session_id == "ses_a"


def test_close_idempotent_and_list_recent(db):
    repo = SessionRepository(db)
    a = repo.create(project_id="p", now=NOW)
    b = repo.create(project_id="p", now=NOW + timedelta(seconds=5))
    repo.close(a.id, now=NOW)
    repo.close(a.id, now=NOW + timedelta(hours=1))
    got = repo.get(a.id)
    assert got.status == SessionStatus.CLOSED and got.closed_at == NOW
    assert [s.id for s in repo.list_recent(10)][0] == b.id
    repo.touch(a.id, now=NOW + timedelta(seconds=60))


def test_close_idle_respects_cutoff_and_unfinished_tasks(db):
    repo = SessionRepository(db)
    tasks = TaskRepository(db)
    old = repo.create(project_id="p", now=NOW - timedelta(days=10))
    busy = repo.create(project_id="p", now=NOW - timedelta(days=10))
    fresh = repo.create(project_id="p", now=NOW)
    t = tasks.create(project_id="p", prompt="x", discord_user_id="u", session_id=busy.id, now=NOW)
    n = repo.close_idle(cutoff=NOW - timedelta(days=7), now=NOW)
    assert n == 1
    assert repo.get(old.id).status == SessionStatus.CLOSED
    assert repo.get(busy.id).status == SessionStatus.OPEN
    assert repo.get(fresh.id).status == SessionStatus.OPEN
    tasks.transition(t.id, TaskStatus.CANCELLED, now=NOW)
    assert repo.close_idle(cutoff=NOW - timedelta(days=7), now=NOW) == 1


def test_attempts_lifecycle_and_clamp(db):
    tasks = TaskRepository(db)
    t = tasks.create(project_id="p", prompt="x", discord_user_id="u", now=NOW)
    repo = FallbackAttemptRepository(db)
    a2 = repo.start(task_id=t.id, session_id=None, attempt_no=2, provider="g", model="m", now=NOW)
    a1 = repo.start(task_id=t.id, session_id=None, attempt_no=1, provider="o", model="m", now=NOW)
    repo.finish(
        a1,
        status=AttemptStatus.FAILED,
        error_class="RATE_LIMITED",
        http_status=429,
        retry_after_ms=10**15,
        now=NOW,
    )
    repo.finish(
        a2,
        status=AttemptStatus.SUCCEEDED,
        error_class=None,
        http_status=None,
        retry_after_ms=None,
        now=NOW,
    )
    got = repo.list_for_task(t.id)
    assert [r.attempt_no for r in got] == [1, 2]
    assert got[0].status == AttemptStatus.FAILED and got[0].retry_after_ms == 2**31 - 1
    assert got[0].http_status == 429 and got[0].finished_at == NOW
    assert got[1].retry_after_ms is None and got[1].status == AttemptStatus.SUCCEEDED


def test_task_set_selection_attach_find_list(db):
    tasks = TaskRepository(db)
    sessions = SessionRepository(db)
    s = sessions.create(project_id="p", now=NOW)
    t1 = tasks.create(project_id="p", prompt="a", discord_user_id="u", now=NOW)
    t2 = tasks.create(
        project_id="p", prompt="b", discord_user_id="u", now=NOW + timedelta(seconds=1)
    )
    sel = tasks.set_selection(t1.id, provider="openrouter", model="m", now=NOW)
    assert sel.cost_class == CostClass.FREE and sel.selected_provider == "openrouter"
    assert tasks.attach_session(t1.id, s.id, now=NOW).session_id == s.id
    assert tasks.find_by_ref(t1.id[:8]).id == t1.id
    assert [t.id for t in tasks.list_recent(5)] == [t2.id, t1.id]
    assert [t.id for t in tasks.list_recent(1)] == [t2.id]
