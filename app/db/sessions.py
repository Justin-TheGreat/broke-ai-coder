from __future__ import annotations

import logging
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from app.db.connection import transaction
from app.timeutil import from_db, from_db_opt, to_db

logger = logging.getLogger(__name__)

_TERMINAL_TASK_STATUSES = ("SUCCEEDED", "FAILED", "CANCELLED")


class SessionStatus(StrEnum):
    OPEN = "OPEN"
    CLOSED = "CLOSED"


@dataclass(frozen=True, slots=True)
class SessionRecord:
    id: str
    opencode_session_id: str | None
    project_id: str
    status: SessionStatus
    created_at: datetime
    updated_at: datetime
    closed_at: datetime | None


class AmbiguousReference(LookupError):
    pass


def _row_to_record(row: sqlite3.Row) -> SessionRecord:
    return SessionRecord(
        id=row["id"],
        opencode_session_id=row["opencode_session_id"],
        project_id=row["project_id"],
        status=SessionStatus(row["status"]),
        created_at=from_db(row["created_at"]),
        updated_at=from_db(row["updated_at"]),
        closed_at=from_db_opt(row["closed_at"]),
    )


class SessionRepository:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def create(self, *, project_id: str, now: datetime) -> SessionRecord:
        sid = str(uuid.uuid4())
        ts = to_db(now)
        with transaction(self._conn):
            self._conn.execute(
                "INSERT INTO sessions (id, opencode_session_id, project_id, status,"
                " created_at, updated_at) VALUES (?, NULL, ?, ?, ?, ?)",
                (sid, project_id, SessionStatus.OPEN.value, ts, ts),
            )
        record = self.get(sid)
        assert record is not None
        return record

    def get(self, session_id: str) -> SessionRecord | None:
        row = self._conn.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
        return None if row is None else _row_to_record(row)

    def find_by_ref(self, ref: str) -> SessionRecord | None:
        exact = self.get(ref)
        if exact is not None:
            return exact
        rows = self._conn.execute(
            "SELECT * FROM sessions WHERE id LIKE ? || '%' LIMIT 2", (ref,)
        ).fetchall()
        if len(rows) > 1:
            raise AmbiguousReference(ref)
        return _row_to_record(rows[0]) if rows else None

    def set_opencode_session_id(
        self, session_id: str, oc_id: str, *, now: datetime
    ) -> SessionRecord:
        with transaction(self._conn):
            cur = self._conn.execute(
                "UPDATE sessions SET opencode_session_id = ?, updated_at = ?"
                " WHERE id = ? AND opencode_session_id IS NULL",
                (oc_id, to_db(now), session_id),
            )
            changed = cur.rowcount
        record = self.get(session_id)
        if record is None:
            raise LookupError(session_id)
        if not changed and record.opencode_session_id != oc_id:
            logger.warning("session already has an opencode session id; new id ignored")
        return record

    def touch(self, session_id: str, *, now: datetime) -> None:
        with transaction(self._conn):
            self._conn.execute(
                "UPDATE sessions SET updated_at = ? WHERE id = ?", (to_db(now), session_id)
            )

    def close(self, session_id: str, *, now: datetime) -> None:
        ts = to_db(now)
        with transaction(self._conn):
            self._conn.execute(
                "UPDATE sessions SET status = ?, closed_at = ?, updated_at = ?"
                " WHERE id = ? AND status = ?",
                (SessionStatus.CLOSED.value, ts, ts, session_id, SessionStatus.OPEN.value),
            )

    def list_recent(self, limit: int) -> list[SessionRecord]:
        rows = self._conn.execute(
            "SELECT * FROM sessions ORDER BY updated_at DESC, id LIMIT ?", (limit,)
        ).fetchall()
        return [_row_to_record(r) for r in rows]

    def close_idle(self, *, cutoff: datetime, now: datetime) -> int:
        terminal = ", ".join("?" for _ in _TERMINAL_TASK_STATUSES)
        ts = to_db(now)
        with transaction(self._conn):
            cur = self._conn.execute(
                "UPDATE sessions SET status = ?, closed_at = ?, updated_at = ?"
                " WHERE status = ? AND updated_at < ? AND NOT EXISTS ("
                f" SELECT 1 FROM tasks t WHERE t.session_id = sessions.id"
                f" AND t.status NOT IN ({terminal}))",
                (
                    SessionStatus.CLOSED.value,
                    ts,
                    ts,
                    SessionStatus.OPEN.value,
                    to_db(cutoff),
                    *_TERMINAL_TASK_STATUSES,
                ),
            )
            return cur.rowcount
