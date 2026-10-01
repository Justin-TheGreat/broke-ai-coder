from __future__ import annotations

import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime

from app.db.connection import transaction
from app.orchestrator.state_machine import (
    TERMINAL_STATUSES,
    TaskStatus,
    ensure_transition,
)
from app.providers.base import CostClass
from app.timeutil import from_db, from_db_opt, to_db, utcnow


@dataclass(frozen=True, slots=True)
class TaskRecord:
    id: str
    project_id: str
    session_id: str | None
    discord_guild_id: str | None
    discord_channel_id: str | None
    discord_user_id: str
    prompt: str
    status: TaskStatus
    selected_provider: str | None
    selected_model: str | None
    cost_class: CostClass | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    last_event_at: datetime
    exit_code: int | None
    error_class: str | None


class TaskNotFound(LookupError):
    pass


class ConcurrentTransition(RuntimeError):
    pass


def _row_to_record(row: sqlite3.Row) -> TaskRecord:
    return TaskRecord(
        id=row["id"],
        project_id=row["project_id"],
        session_id=row["session_id"],
        discord_guild_id=row["discord_guild_id"],
        discord_channel_id=row["discord_channel_id"],
        discord_user_id=row["discord_user_id"],
        prompt=row["prompt"],
        status=TaskStatus(row["status"]),
        selected_provider=row["selected_provider"],
        selected_model=row["selected_model"],
        cost_class=CostClass(row["cost_class"]) if row["cost_class"] is not None else None,
        created_at=from_db(row["created_at"]),
        started_at=from_db_opt(row["started_at"]),
        finished_at=from_db_opt(row["finished_at"]),
        last_event_at=from_db(row["last_event_at"]),
        exit_code=row["exit_code"],
        error_class=row["error_class"],
    )


class TaskRepository:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def create(
        self,
        *,
        project_id: str,
        prompt: str,
        discord_user_id: str,
        discord_guild_id: str | None = None,
        discord_channel_id: str | None = None,
        session_id: str | None = None,
        task_id: str | None = None,
        now: datetime | None = None,
    ) -> TaskRecord:
        tid = task_id or str(uuid.uuid4())
        ts = to_db(now or utcnow())
        with transaction(self._conn):
            self._conn.execute(
                "INSERT INTO tasks (id, project_id, session_id, discord_guild_id,"
                " discord_channel_id, discord_user_id, prompt, status, created_at, last_event_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    tid,
                    project_id,
                    session_id,
                    discord_guild_id,
                    discord_channel_id,
                    discord_user_id,
                    prompt,
                    TaskStatus.QUEUED.value,
                    ts,
                    ts,
                ),
            )
        record = self.get(tid)
        assert record is not None
        return record

    def get(self, task_id: str) -> TaskRecord | None:
        row = self._conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        return None if row is None else _row_to_record(row)

    def transition(
        self,
        task_id: str,
        target: TaskStatus,
        *,
        now: datetime | None = None,
        error_class: str | None = None,
        exit_code: int | None = None,
    ) -> TaskRecord:
        ts = to_db(now or utcnow())
        with transaction(self._conn):
            row = self._conn.execute("SELECT status FROM tasks WHERE id = ?", (task_id,)).fetchone()
            if row is None:
                raise TaskNotFound(task_id)
            current = TaskStatus(row["status"])
            ensure_transition(current, target)

            sets = ["status = ?", "last_event_at = ?"]
            params: list[object] = [target.value, ts]
            if target == TaskStatus.RUNNING:
                sets.append("started_at = COALESCE(started_at, ?)")
                params.append(ts)
            if target in TERMINAL_STATUSES:
                sets.append("finished_at = ?")
                params.append(ts)
            if error_class is not None:
                sets.append("error_class = ?")
                params.append(error_class)
            if exit_code is not None:
                sets.append("exit_code = ?")
                params.append(exit_code)
            params.extend([task_id, current.value])
            cur = self._conn.execute(
                f"UPDATE tasks SET {', '.join(sets)} WHERE id = ? AND status = ?",
                params,
            )
            if cur.rowcount == 0:
                raise ConcurrentTransition(task_id)
        record = self.get(task_id)
        assert record is not None
        return record

    def list_unfinished(self) -> list[TaskRecord]:
        terminal = ", ".join("?" for _ in TERMINAL_STATUSES)
        rows = self._conn.execute(
            f"SELECT * FROM tasks WHERE status NOT IN ({terminal}) ORDER BY created_at, id",
            [s.value for s in TERMINAL_STATUSES],
        ).fetchall()
        return [_row_to_record(r) for r in rows]
