from __future__ import annotations

import asyncio
import logging
import sqlite3
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime

from app.config.models import AppConfig
from app.db.connection import db_size_bytes, open_database
from app.db.migrations import migrate
from app.db.retention import CleanupResult, run_retention_cleanup
from app.db.tasks import TaskRepository
from app.runtime.interfaces import ChatFrontend, NullFrontend
from app.timeutil import to_db, utcnow

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class TaskSubmission:
    project_id: str
    prompt: str
    discord_user_id: str
    discord_guild_id: str | None = None
    discord_channel_id: str | None = None


@dataclass(frozen=True, slots=True)
class ControllerEvent:
    name: str
    at: datetime
    task_id: str | None = None
    fields: Mapping[str, str | int | float | bool | None] = field(default_factory=dict)


TaskHandler = Callable[["AgentController", TaskSubmission], Awaitable[None]]


def _format_event(event: ControllerEvent) -> str:
    extra = " ".join(f"{k}={v}" for k, v in event.fields.items())
    base = f"event={event.name} task_id={event.task_id}"
    return f"{base} {extra}" if extra else base


class AgentController:
    def __init__(
        self,
        config: AppConfig,
        *,
        frontend: ChatFrontend | None = None,
        task_handler: TaskHandler | None = None,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self._config = config
        self._frontend: ChatFrontend = frontend if frontend is not None else NullFrontend()
        self._task_handler: TaskHandler = task_handler or self._default_handler
        self._clock = clock
        self.conn: sqlite3.Connection | None = None
        self._task_queue: asyncio.Queue[TaskSubmission] = asyncio.Queue(
            maxsize=config.runtime.task_queue_maxsize
        )
        self._event_queue: asyncio.Queue[ControllerEvent] = asyncio.Queue(
            maxsize=config.runtime.event_queue_maxsize
        )
        self._stop_event = asyncio.Event()
        self._first_cleanup_done = asyncio.Event()
        self._tasks: list[asyncio.Task[None]] = []
        self._started = False
        self._stopped = False
        self.event_dropped_count = 0
        self._last_cleanup_at: datetime | None = None

    # ---- lifecycle -------------------------------------------------

    async def start(self) -> None:
        if self._started:
            raise RuntimeError("controller already started")
        self._started = True
        db = self._config.database
        self.conn = open_database(db.path, busy_timeout_ms=db.busy_timeout_ms)
        try:
            migrate(self.conn)
            self._tasks = [
                asyncio.create_task(self._worker_loop(), name="task-worker"),
                asyncio.create_task(self._event_loop(), name="event-consumer"),
                asyncio.create_task(self._cleanup_loop(), name="cleanup-loop"),
            ]
            await self._frontend.start(self)
        except BaseException:
            await self._teardown()
            raise

    async def stop(self) -> None:
        if self._stopped:
            return
        self._stopped = True
        self._stop_event.set()
        try:
            await self._frontend.stop()
        except Exception:
            logger.exception("frontend stop failed")
        if self._tasks:
            try:
                await asyncio.wait_for(
                    self._task_queue.join(), timeout=self._config.runtime.shutdown_timeout_s
                )
            except TimeoutError:
                logger.warning("shutdown timeout waiting for task queue to drain")
        await self._teardown()

    async def _teardown(self) -> None:
        for t in self._tasks:
            t.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks = []
        while True:
            try:
                event = self._event_queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            logger.info(_format_event(event))
        if self.conn is not None:
            self.conn.close()
            self.conn = None

    async def run_forever(self) -> None:
        await self.start()
        try:
            await self._stop_event.wait()
        finally:
            await self.stop()

    async def run_once(self) -> None:
        await self.start()
        try:
            await self._first_cleanup_done.wait()
            self.request_stop()
        finally:
            await self.stop()

    def request_stop(self) -> None:
        self._stop_event.set()

    # ---- queues ----------------------------------------------------

    def submit_task(self, submission: TaskSubmission) -> bool:
        if self._stopped:
            logger.warning("controller stopped; submission rejected")
            return False
        try:
            self._task_queue.put_nowait(submission)
        except asyncio.QueueFull:
            logger.warning("task queue full; submission rejected")
            return False
        return True

    def emit(self, event: ControllerEvent) -> bool:
        try:
            self._event_queue.put_nowait(event)
        except asyncio.QueueFull:
            self.event_dropped_count += 1
            return False
        return True

    # ---- workers ---------------------------------------------------

    async def _default_handler(self, controller: AgentController, sub: TaskSubmission) -> None:
        assert self.conn is not None
        record = TaskRepository(self.conn).create(
            project_id=sub.project_id,
            prompt=sub.prompt,
            discord_user_id=sub.discord_user_id,
            discord_guild_id=sub.discord_guild_id,
            discord_channel_id=sub.discord_channel_id,
            now=self._clock(),
        )
        self.emit(ControllerEvent("task_queued", at=self._clock(), task_id=record.id))

    async def _worker_loop(self) -> None:
        while True:
            sub = await self._task_queue.get()
            try:
                await self._task_handler(self, sub)
            except Exception as e:
                logger.exception("task handler failed")
                self.emit(
                    ControllerEvent(
                        "task_handler_error", at=self._clock(), fields={"error": type(e).__name__}
                    )
                )
            finally:
                self._task_queue.task_done()

    async def _event_loop(self) -> None:
        while True:
            event = await self._event_queue.get()
            try:
                logger.info(_format_event(event))
            finally:
                self._event_queue.task_done()

    async def _cleanup_loop(self) -> None:
        interval = self._config.database.cleanup_interval_hours * 3600
        while True:
            try:
                await self.run_cleanup_now()
            except Exception:
                logger.exception("retention cleanup failed")
            self._first_cleanup_done.set()
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=interval)
                return
            except TimeoutError:
                continue

    async def run_cleanup_now(self) -> CleanupResult:
        db = self._config.database
        now = self._clock()

        def _work() -> CleanupResult:
            conn = open_database(db.path, busy_timeout_ms=db.busy_timeout_ms)
            try:
                return run_retention_cleanup(
                    conn,
                    now=now,
                    retention_days=db.retention_days,
                    batch_size=db.cleanup_batch_size,
                )
            finally:
                conn.close()

        result = await asyncio.to_thread(_work)
        self._last_cleanup_at = now
        fields: dict[str, str | int | float | bool | None] = dict(result.deleted)
        fields["db_size_bytes"] = result.db_size_bytes
        self.emit(ControllerEvent("retention_cleanup", at=now, fields=fields))
        return result

    # ---- metrics ---------------------------------------------------

    def metrics(self) -> dict[str, int | str | None]:
        return {
            "task_queue_depth": self._task_queue.qsize(),
            "event_queue_depth": self._event_queue.qsize(),
            "event_dropped_count": self.event_dropped_count,
            "last_cleanup_at": (
                to_db(self._last_cleanup_at) if self._last_cleanup_at is not None else None
            ),
            "sqlite_db_bytes": db_size_bytes(self.conn) if self.conn is not None else None,
        }
