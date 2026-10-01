from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from app.db.connection import transaction
from app.timeutil import from_db, from_db_opt, to_db

_INT32_MAX = 2**31 - 1


class AttemptStatus(StrEnum):
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    TIMED_OUT = "TIMED_OUT"
    CANCELLED = "CANCELLED"
    EXECUTOR_UNAVAILABLE = "EXECUTOR_UNAVAILABLE"


@dataclass(frozen=True, slots=True)
class AttemptRecord:
    id: int
    task_id: str
    session_id: str | None
    attempt_no: int
    provider: str
    model: str
    status: AttemptStatus
    error_class: str | None
    http_status: int | None
    retry_after_ms: int | None
    started_at: datetime
    finished_at: datetime | None


def _row_to_record(row: sqlite3.Row) -> AttemptRecord:
    return AttemptRecord(
        id=row["id"],
        task_id=row["task_id"],
        session_id=row["session_id"],
        attempt_no=row["attempt_no"],
        provider=row["provider"],
        model=row["model"],
        status=AttemptStatus(row["status"]),
        error_class=row["error_class"],
        http_status=row["http_status"],
        retry_after_ms=row["retry_after_ms"],
        started_at=from_db(row["started_at"]),
        finished_at=from_db_opt(row["finished_at"]),
    )


class FallbackAttemptRepository:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def start(
        self,
        *,
        task_id: str,
        session_id: str | None,
        attempt_no: int,
        provider: str,
        model: str,
        now: datetime,
    ) -> int:
        with transaction(self._conn):
            cur = self._conn.execute(
                "INSERT INTO fallback_attempts (task_id, session_id, attempt_no, provider,"
                " model, status, started_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    task_id,
                    session_id,
                    attempt_no,
                    provider,
                    model,
                    AttemptStatus.RUNNING.value,
                    to_db(now),
                ),
            )
        assert cur.lastrowid is not None
        return cur.lastrowid

    def finish(
        self,
        attempt_id: int,
        *,
        status: AttemptStatus,
        error_class: str | None,
        http_status: int | None,
        retry_after_ms: int | None,
        now: datetime,
    ) -> None:
        if retry_after_ms is not None:
            retry_after_ms = min(max(int(retry_after_ms), 0), _INT32_MAX)
        with transaction(self._conn):
            self._conn.execute(
                "UPDATE fallback_attempts SET status = ?, error_class = ?, http_status = ?,"
                " retry_after_ms = ?, finished_at = ? WHERE id = ?",
                (status.value, error_class, http_status, retry_after_ms, to_db(now), attempt_id),
            )

    def list_for_task(self, task_id: str) -> list[AttemptRecord]:
        rows = self._conn.execute(
            "SELECT * FROM fallback_attempts WHERE task_id = ? ORDER BY attempt_no", (task_id,)
        ).fetchall()
        return [_row_to_record(r) for r in rows]
