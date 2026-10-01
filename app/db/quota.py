from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from datetime import datetime

from app.db.connection import transaction
from app.providers.base import QuotaConfidence, QuotaRecord, QuotaUnit
from app.quota.normalize import validate_record
from app.timeutil import from_db, from_db_opt, to_db, to_db_opt

_INSERT = (
    "INSERT INTO quota_snapshots (provider, model, quota_window, limit_value, used, remaining,"
    " unit, confidence, reset_at, observed_at, source) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
)


def _params(r: QuotaRecord) -> tuple[object, ...]:
    return (
        r.provider,
        r.model,
        r.window,
        r.limit,
        r.used,
        r.remaining,
        r.unit.value,
        r.confidence.value,
        to_db_opt(r.reset_at),
        to_db(r.observed_at),
        r.source,
    )


def _row_to_record(row: sqlite3.Row) -> QuotaRecord:
    return QuotaRecord(
        provider=row["provider"],
        model=row["model"],
        window=row["quota_window"],
        unit=QuotaUnit(row["unit"]),
        confidence=QuotaConfidence(row["confidence"]),
        observed_at=from_db(row["observed_at"]),
        source=row["source"],
        limit=row["limit_value"],
        used=row["used"],
        remaining=row["remaining"],
        reset_at=from_db_opt(row["reset_at"]),
    )


class QuotaSnapshotRepository:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def insert(self, record: QuotaRecord) -> int:
        validate_record(record)
        with transaction(self._conn):
            cur = self._conn.execute(_INSERT, _params(record))
        assert cur.lastrowid is not None
        return cur.lastrowid

    def insert_many(self, records: Sequence[QuotaRecord]) -> list[int]:
        for r in records:
            validate_record(r)
        ids: list[int] = []
        with transaction(self._conn):
            for r in records:
                cur = self._conn.execute(_INSERT, _params(r))
                assert cur.lastrowid is not None
                ids.append(cur.lastrowid)
        return ids

    def latest(self, now: datetime) -> list[QuotaRecord]:
        now_s = to_db(now)
        rows = self._conn.execute(
            "SELECT q.* FROM quota_snapshots q WHERE q.id = ("
            " SELECT s.id FROM quota_snapshots s"
            " WHERE s.provider = q.provider AND COALESCE(s.model, '') = COALESCE(q.model, '')"
            " AND s.quota_window = q.quota_window AND s.unit = q.unit"
            " ORDER BY s.observed_at DESC, s.id DESC LIMIT 1)"
            " AND NOT (q.reset_at IS NOT NULL AND q.reset_at <= ?)"
            " ORDER BY q.provider, q.model IS NOT NULL, q.model, q.quota_window, q.unit",
            (now_s,),
        ).fetchall()
        return [_row_to_record(r) for r in rows]
