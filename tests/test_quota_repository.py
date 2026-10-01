from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

import pytest
from conftest import NOW

from app.db.connection import open_database
from app.db.migrations import migrate
from app.db.quota import QuotaSnapshotRepository
from app.providers.base import QuotaConfidence, QuotaRecord, QuotaUnit


def rec(**kw):
    base = dict(
        provider="p",
        model=None,
        window="day",
        unit=QuotaUnit.REQUESTS,
        confidence=QuotaConfidence.EXACT,
        observed_at=NOW,
        source="p:hdr",
        limit=10,
        used=3,
        remaining=7,
        reset_at=NOW + timedelta(hours=1),
    )
    base.update(kw)
    return QuotaRecord(**base)


def test_round_trip(db):
    repo = QuotaSnapshotRepository(db)
    a = rec()
    b = rec(
        provider="q",
        model="m",
        unit=QuotaUnit.USD,
        window="total",
        limit=10.5,
        used=2.25,
        remaining=8.25,
        reset_at=None,
    )
    c = rec(
        provider="r",
        confidence=QuotaConfidence.UNKNOWN,
        limit=None,
        used=None,
        remaining=None,
        reset_at=None,
    )
    ids = repo.insert_many([a, b, c])
    assert len(ids) == 3
    assert repo.insert(rec(provider="s")) > ids[-1]
    got = {r.provider: r for r in repo.latest(NOW)}
    assert got["p"] == a and got["q"] == b and got["r"] == c
    assert isinstance(got["p"].limit, int)
    assert isinstance(got["q"].limit, float)


def test_latest_only_newest_per_key(db):
    repo = QuotaSnapshotRepository(db)
    repo.insert(rec(observed_at=NOW - timedelta(minutes=5), remaining=9))
    repo.insert(rec(remaining=1))
    repo.insert(rec(model="m", remaining=5))
    out = repo.latest(NOW)
    assert [(r.model, r.remaining) for r in out] == [(None, 1), ("m", 5)]


def test_reset_at_boundary_excluded(db):
    repo = QuotaSnapshotRepository(db)
    repo.insert(rec(reset_at=NOW))
    repo.insert(rec(provider="q", reset_at=NOW + timedelta(microseconds=1)))
    assert [r.provider for r in repo.latest(NOW)] == ["q"]


def test_insert_many_all_or_nothing(db):
    repo = QuotaSnapshotRepository(db)
    with pytest.raises(ValueError):
        repo.insert_many([rec(), replace(rec(provider="bad"), limit=-1)])
    assert db.execute("SELECT COUNT(*) FROM quota_snapshots").fetchone()[0] == 0


def test_insert_validates(db):
    with pytest.raises(ValueError):
        QuotaSnapshotRepository(db).insert(rec(source="estimate:x"))


def test_survives_reopen(tmp_path):
    path = tmp_path / "q.db"
    conn = open_database(path)
    try:
        migrate(conn)
        QuotaSnapshotRepository(conn).insert(rec())
    finally:
        conn.close()
    conn = open_database(path)
    try:
        assert QuotaSnapshotRepository(conn).latest(NOW) == [rec()]
    finally:
        conn.close()
