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
        {"cost_class": CostClass.PAID},
        {"cost_class": CostClass.PAID, "estimated_cost_usd": -1.0},
        {"estimated_cost_usd": float("inf")},
        {"estimated_cost_usd": float("nan")},
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


def test_paid_spend_ignores_free(db):
    repo = UsageRepository(db)
    repo.record(ev(estimated_cost_usd=9.0))
    repo.record(ev(cost_class=CostClass.PAID, estimated_cost_usd=0.25))
    repo.record(ev(cost_class=CostClass.PAID, estimated_cost_usd=0.5, occurred_at=NOW - 2 * H))
    s = repo.paid_spend(since=NOW - H, until=NOW)
    assert (s.total_usd, s.missing_cost_events) == (0.25, 0)
    assert repo.paid_spend(since=NOW - 3 * H, until=NOW).total_usd == 0.75


def test_paid_spend_missing_cost_counted(db):
    db.execute(
        "INSERT INTO usage_events (provider, model, cost_class, status, occurred_at)"
        " VALUES ('p', 'm', 'PAID', 'success', ?)",
        (NOW.isoformat(timespec="microseconds"),),
    )
    s = UsageRepository(db).paid_spend(since=NOW - H, until=NOW)
    assert (s.total_usd, s.missing_cost_events) == (0.0, 1)


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
