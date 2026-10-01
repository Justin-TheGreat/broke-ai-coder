from __future__ import annotations

import sqlite3
from dataclasses import replace
from datetime import datetime, timedelta

import pytest
from conftest import NOW

from app.db.tasks import TaskRepository
from app.db.usage import UsageEvent, UsageRepository, UsageStatus
from app.providers.base import CostClass


def ev(**kw):
    base = dict(
        provider="p",
        model="m",
        cost_class=CostClass.FREE,
        status=UsageStatus.SUCCESS,
        occurred_at=NOW,
        input_tokens=10,
        output_tokens=5,
    )
    base.update(kw)
    return UsageEvent(**base)


H = timedelta(hours=1)


def test_totals_count_all_statuses_and_filters(db):
    repo = UsageRepository(db)
    repo.record(ev())
    repo.record(ev(status=UsageStatus.ERROR, input_tokens=None, output_tokens=None))
    repo.record(ev(status=UsageStatus.UNKNOWN_OUTCOME, input_tokens=1, output_tokens=None))
    repo.record(ev(model="other", occurred_at=NOW - H))
    repo.record(ev(provider="q"))
    t = repo.totals("p", model="m", since=NOW - 2 * H, until=NOW)
    assert (t.requests, t.input_tokens, t.output_tokens) == (3, 11, 5)
    assert t.events_missing_tokens == 2
    assert t.earliest == NOW
    allm = repo.totals("p", since=NOW - 2 * H, until=NOW)
    assert allm.requests == 4
    assert allm.earliest == NOW - H


def test_totals_inclusive_bounds(db):
    repo = UsageRepository(db)
    repo.record(ev(occurred_at=NOW - H))
    repo.record(ev(occurred_at=NOW))
    repo.record(ev(occurred_at=NOW + timedelta(microseconds=1)))
    repo.record(ev(occurred_at=NOW - H - timedelta(microseconds=1)))
    assert repo.totals("p", since=NOW - H, until=NOW).requests == 2


def test_totals_empty(db):
    t = UsageRepository(db).totals("p", since=NOW - H, until=NOW)
    assert (t.requests, t.input_tokens, t.events_missing_tokens, t.earliest) == (0, 0, 0, None)


@pytest.mark.parametrize(
    "bad",
    [
        {"cost_class": "PAID"},
        {"input_tokens": -1},
        {"output_tokens": True},
        {"input_tokens": 1.5},
        {"occurred_at": datetime(2026, 1, 1)},
        {"provider": ""},
        {"model": ""},
    ],
)
def test_record_validation(db, bad):
    with pytest.raises(ValueError):
        UsageRepository(db).record(replace(ev(), **bad))
    assert db.execute("SELECT COUNT(*) FROM usage_events").fetchone()[0] == 0


def test_unknown_task_id_integrity_error(db):
    with pytest.raises(sqlite3.IntegrityError):
        UsageRepository(db).record(ev(task_id="nope"))


def test_known_task_id_ok(db):
    task = TaskRepository(db).create(project_id="p", prompt="x", discord_user_id="u")
    UsageRepository(db).record(ev(task_id=task.id, request_id="r1"))


def test_db_rejects_paid_usage_insert(db):
    with pytest.raises(sqlite3.IntegrityError, match="free-only"):
        db.execute(
            "INSERT INTO usage_events (provider, model, cost_class, status, occurred_at)"
            " VALUES ('p', 'm', 'PAID', 'success', ?)",
            (NOW.isoformat(timespec="microseconds"),),
        )
    assert db.execute("SELECT COUNT(*) FROM usage_events").fetchone()[0] == 0


def test_db_rejects_update_to_paid(db):
    UsageRepository(db).record(ev())
    task = TaskRepository(db).create(project_id="p", prompt="x", discord_user_id="u")
    with pytest.raises(sqlite3.IntegrityError, match="free-only"):
        db.execute("UPDATE usage_events SET cost_class = 'PAID'")
    with pytest.raises(sqlite3.IntegrityError, match="free-only"):
        db.execute("UPDATE tasks SET cost_class = 'PAID' WHERE id = ?", (task.id,))
    db.execute("UPDATE tasks SET cost_class = 'FREE' WHERE id = ?", (task.id,))


def test_db_rejects_paid_policy_row(db):
    with pytest.raises(sqlite3.IntegrityError, match="free-only"):
        db.execute(
            "INSERT INTO provider_model_policy"
            " (provider, model, priority, enabled, cost_class, created_at, updated_at)"
            " VALUES ('openrouter', 'x', 0, 1, 'PAID', 't', 't')"
        )


def test_daily_totals_excludes_yesterday(db):
    repo = UsageRepository(db)
    midnight = NOW.replace(hour=0, minute=0, second=0, microsecond=0)
    repo.record(ev(occurred_at=midnight - timedelta(microseconds=1)))
    repo.record(ev(occurred_at=midnight))
    repo.record(ev(provider="a", model="z"))
    repo.record(ev(provider="a", model="b", input_tokens=None))
    out = repo.daily_totals(NOW)
    assert [(p, m) for p, m, _ in out] == [("a", "b"), ("a", "z"), ("p", "m")]
    assert out[2][2].requests == 1
    assert out[0][2].events_missing_tokens == 1
