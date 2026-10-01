from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta

from app.db.connection import db_size_bytes, transaction
from app.timeutil import to_db, utcnow

RETENTION_DAYS_DEFAULT = 60

_PROTECTED = (
    "SELECT t.id FROM tasks t LEFT JOIN sessions s ON s.id = t.session_id "
    "WHERE t.status NOT IN ('SUCCEEDED','FAILED','CANCELLED') OR s.status = 'OPEN'"
)

# (table, condition) in processing order.
_STEPS: tuple[tuple[str, str], ...] = (
    (
        "usage_events",
        f"occurred_at < :cutoff AND (task_id IS NULL OR task_id NOT IN ({_PROTECTED}))",
    ),
    (
        "provider_events",
        f"created_at < :cutoff AND (task_id IS NULL OR task_id NOT IN ({_PROTECTED}))",
    ),
    ("fallback_attempts", f"started_at < :cutoff AND task_id NOT IN ({_PROTECTED})"),
    ("quota_snapshots", "observed_at < :cutoff"),
    (
        "tasks",
        "status IN ('SUCCEEDED','FAILED','CANCELLED') "
        "AND COALESCE(finished_at, created_at) < :cutoff "
        "AND (session_id IS NULL OR session_id NOT IN "
        "(SELECT id FROM sessions WHERE status = 'OPEN'))",
    ),
    (
        "sessions",
        "status = 'CLOSED' AND COALESCE(closed_at, updated_at) < :cutoff "
        "AND NOT EXISTS (SELECT 1 FROM tasks WHERE tasks.session_id = sessions.id)",
    ),
)


@dataclass(frozen=True, slots=True)
class CleanupResult:
    cutoff: datetime
    deleted: dict[str, int]
    db_size_bytes: int


def run_retention_cleanup(
    conn: sqlite3.Connection,
    *,
    now: datetime | None = None,
    retention_days: int = RETENTION_DAYS_DEFAULT,
    batch_size: int = 500,
    checkpoint: bool = True,
) -> CleanupResult:
    if retention_days < 1:
        raise ValueError("retention_days must be >= 1")
    if batch_size < 1:
        raise ValueError("batch_size must be >= 1")
    cutoff = (now or utcnow()) - timedelta(days=retention_days)
    params = {"cutoff": to_db(cutoff), "batch": batch_size}
    deleted: dict[str, int] = {}
    for table, cond in _STEPS:
        total = 0
        sql = f"DELETE FROM {table} WHERE id IN (SELECT id FROM {table} WHERE {cond} LIMIT :batch)"
        while True:
            with transaction(conn):
                n = conn.execute(sql, params).rowcount
            total += n
            if n < batch_size:
                break
        deleted[table] = total
    if checkpoint and sum(deleted.values()) > 0:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    return CleanupResult(cutoff=cutoff, deleted=deleted, db_size_bytes=db_size_bytes(conn))


def run_vacuum(conn: sqlite3.Connection) -> None:
    conn.execute("VACUUM")
