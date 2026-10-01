from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from conftest import NOW

from app.db.connection import open_database
from app.db.migrations import migrate
from app.db.tasks import TaskNotFound, TaskRepository
from app.orchestrator.state_machine import InvalidTransition, TaskStatus

S = TaskStatus


def mk(repo, **kw):
    return repo.create(project_id="p", prompt="hi", discord_user_id="u", now=NOW, **kw)


def sec(n):
    return NOW + timedelta(seconds=n)


def test_create_queued(db):
    t = mk(TaskRepository(db), discord_guild_id="123")
    assert t.status == S.QUEUED
    assert t.created_at == t.last_event_at == NOW
    assert t.started_at is None and t.finished_at is None
    assert t.discord_guild_id == "123"


def test_happy_path_timestamps(db):
    repo = TaskRepository(db)
    t = mk(repo)
    repo.transition(t.id, S.ROUTING, now=sec(1))
    r = repo.transition(t.id, S.RUNNING, now=sec(2))
    assert r.started_at == sec(2)
    r = repo.transition(t.id, S.SUCCEEDED, now=sec(3), exit_code=0)
    assert r.finished_at == sec(3) and r.started_at == sec(2)
    assert r.exit_code == 0
    assert r.last_event_at == sec(3)


def test_invalid_transition_leaves_row(db):
    repo = TaskRepository(db)
    t = mk(repo)
    with pytest.raises(InvalidTransition):
        repo.transition(t.id, S.SUCCEEDED, now=sec(5))
    assert repo.get(t.id) == t
    assert not db.in_transaction


def test_terminal_has_no_exit(db):
    repo = TaskRepository(db)
    t = mk(repo)
    repo.transition(t.id, S.CANCELLED, now=NOW)
    with pytest.raises(InvalidTransition):
        repo.transition(t.id, S.QUEUED, now=NOW)


def test_missing_task(db):
    repo = TaskRepository(db)
    assert repo.get("nope") is None
    with pytest.raises(TaskNotFound):
        repo.transition("nope", S.ROUTING)


def test_naive_now_rejected(db):
    repo = TaskRepository(db)
    with pytest.raises(ValueError):
        repo.create(project_id="p", prompt="x", discord_user_id="u", now=datetime(2026, 1, 1))


def test_persists_across_reopen(tmp_path):
    path = tmp_path / "r.db"
    conn = open_database(path)
    migrate(conn)
    repo = TaskRepository(conn)
    t = mk(repo)
    repo.transition(t.id, S.ROUTING, now=NOW)
    conn.close()
    conn = open_database(path)
    try:
        migrate(conn)
        repo2 = TaskRepository(conn)
        got = repo2.get(t.id)
        assert got is not None and got.status == S.ROUTING
        assert [x.id for x in repo2.list_unfinished()] == [t.id]
    finally:
        conn.close()


def test_list_unfinished_excludes_terminal(db):
    repo = TaskRepository(db)
    a = mk(repo)
    b = mk(repo)
    repo.transition(b.id, S.CANCELLED, now=NOW)
    assert [x.id for x in repo.list_unfinished()] == [a.id]


def test_fallback_keeps_started_at(db):
    repo = TaskRepository(db)
    t = mk(repo)
    repo.transition(t.id, S.ROUTING, now=sec(1))
    first = repo.transition(t.id, S.RUNNING, now=sec(2))
    repo.transition(t.id, S.ROUTING, now=sec(3))
    again = repo.transition(t.id, S.RUNNING, now=sec(4))
    assert again.started_at == first.started_at == sec(2)


def test_error_class_set(db):
    repo = TaskRepository(db)
    t = mk(repo)
    repo.transition(t.id, S.ROUTING, now=NOW)
    r = repo.transition(t.id, S.FAILED, now=NOW, error_class="UNKNOWN")
    assert r.error_class == "UNKNOWN"
