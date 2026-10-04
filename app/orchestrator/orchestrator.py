"""Task orchestrator: route -> run via OpenCode -> fall back on provider failures.

Assumption B10: each ``step_finish`` event is one LLM request, so for quota estimates an
attempt counts ``max(steps, 1)`` requests.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import sqlite3
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import TYPE_CHECKING

from app.config.models import AppConfig
from app.db.attempts import AttemptStatus, FallbackAttemptRepository
from app.db.sessions import SessionRecord, SessionRepository, SessionStatus
from app.db.tasks import ConcurrentTransition, TaskNotFound, TaskRecord, TaskRepository
from app.db.usage import UsageEvent, UsageRepository, UsageStatus
from app.opencode.config_check import allowed_small_models
from app.orchestrator.prompt import PROMPT_MAX_CHARS, sanitize_prompt
from app.orchestrator.state_machine import (
    TERMINAL_STATUSES,
    InvalidTransition,
    TaskStatus,
)
from app.providers.base import STOP_ERRORS, CostClass, ErrorClass, ProviderAdapter
from app.quota.cooldown import CooldownManager
from app.quota.state import ProviderObservation, build_router_state, refresh_all, route_with_state
from app.router.types import (
    FailedAttempt,
    NoEligibleProvider,
    NoEligibleReason,
    RequiredCapabilities,
    RouteRequest,
    RouterState,
    Selected,
)
from app.runtime.daemon import ControllerEvent, TaskSubmission
from app.runtime.interfaces import (
    AgentExecutor,
    ExecutionOutcome,
    ExecutionResult,
    ExecutorEvent,
    ExecutorEventKind,
    SmallModelCheck,
)
from app.timeutil import utcnow

if TYPE_CHECKING:
    from app.runtime.daemon import AgentController

__all__ = [
    "FAIL_DAEMON_SHUTDOWN",
    "FAIL_INTERRUPTED_BY_RESTART",
    "FAIL_OPENCODE_UNAVAILABLE",
    "FAIL_OPENCODE_UNSAFE_CONFIG",
    "PROMPT_MAX_CHARS",
    "CancelResult",
    "Orchestrator",
    "RunningTask",
    "sanitize_prompt",
]

logger = logging.getLogger(__name__)

FAIL_OPENCODE_UNAVAILABLE = "OPENCODE_UNAVAILABLE"
FAIL_OPENCODE_UNSAFE_CONFIG = "OPENCODE_UNSAFE_CONFIG"
FAIL_INTERRUPTED_BY_RESTART = "INTERRUPTED_BY_RESTART"
FAIL_DAEMON_SHUTDOWN = "DAEMON_SHUTDOWN"

_MAX_USAGE_EVENTS = 500
_FieldValue = str | int | float | bool | None


@dataclass(frozen=True, slots=True)
class RunningTask:
    task_id: str
    session_id: str
    provider: str | None
    model: str | None
    attempt_no: int
    opencode_session_id: str | None
    started_at: datetime


class CancelResult(StrEnum):
    CANCELLED = "CANCELLED"  # was QUEUED, now CANCELLED
    CANCELLING = "CANCELLING"  # running; cancel signalled
    NOT_FOUND = "NOT_FOUND"
    NOT_CANCELLABLE = "NOT_CANCELLABLE"  # already terminal


_ATTEMPT_STATUS = {
    ExecutionOutcome.SUCCEEDED: AttemptStatus.SUCCEEDED,
    ExecutionOutcome.FAILED: AttemptStatus.FAILED,
    ExecutionOutcome.TIMED_OUT: AttemptStatus.TIMED_OUT,
    ExecutionOutcome.CANCELLED: AttemptStatus.CANCELLED,
    ExecutionOutcome.EXECUTOR_UNAVAILABLE: AttemptStatus.EXECUTOR_UNAVAILABLE,
}


class Orchestrator:
    def __init__(
        self,
        config: AppConfig,
        *,
        executor: AgentExecutor,
        adapters: Mapping[str, ProviderAdapter],
        cooldowns: CooldownManager | None = None,
        environ: Mapping[str, str] | None = None,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self._config = config
        self._executor = executor
        self._adapters = adapters
        self._clock = clock
        self._cooldowns = cooldowns or CooldownManager(config.cooldown, clock=clock)
        self._environ = environ
        self._observations: dict[str, ProviderObservation] = {}
        self._last_refresh: datetime | None = None
        self._small_model_ok_at: datetime | None = None
        self._controller: AgentController | None = None
        self._shutting_down = False
        self._cancel_event: asyncio.Event | None = None
        self._current_task_id: str | None = None
        self._running: RunningTask | None = None

    # ---- properties ------------------------------------------------

    @property
    def running(self) -> RunningTask | None:
        return self._running

    @property
    def executor(self) -> AgentExecutor:
        return self._executor

    @property
    def cooldowns(self) -> CooldownManager:
        return self._cooldowns

    @property
    def observations(self) -> Mapping[str, ProviderObservation]:
        return self._observations

    # ---- lifecycle -------------------------------------------------

    def bind(self, controller: AgentController) -> None:
        self._controller = controller

    def recover_interrupted(self) -> int:
        controller = self._controller
        if controller is None or controller.conn is None:
            return 0
        tasks = TaskRepository(controller.conn)
        count = 0
        for rec in tasks.list_unfinished():
            target = TaskStatus.CANCELLED if rec.status == TaskStatus.QUEUED else TaskStatus.FAILED
            try:
                tasks.transition(
                    rec.id,
                    target,
                    now=self._clock(),
                    error_class=FAIL_INTERRUPTED_BY_RESTART,
                )
                count += 1
            except (InvalidTransition, ConcurrentTransition, TaskNotFound, sqlite3.Error) as e:
                logger.warning("restart recovery failed: %s", type(e).__name__)
        if count:
            logger.warning("restart recovery: %d interrupted task(s) closed", count)
        return count

    def begin_shutdown(self) -> None:
        self._shutting_down = True
        if self._cancel_event is not None:
            self._cancel_event.set()

    # ---- cancel ----------------------------------------------------

    def request_cancel(self, task_id: str) -> CancelResult:
        controller = self._controller
        if controller is None or controller.conn is None:
            return CancelResult.NOT_FOUND
        tasks = TaskRepository(controller.conn)
        rec = tasks.get(task_id)
        if rec is None:
            return CancelResult.NOT_FOUND
        if rec.status in TERMINAL_STATUSES:
            return CancelResult.NOT_CANCELLABLE
        if task_id == self._current_task_id and self._cancel_event is not None:
            self._cancel_event.set()
            return CancelResult.CANCELLING
        if rec.status == TaskStatus.QUEUED:
            try:
                done = tasks.transition(task_id, TaskStatus.CANCELLED, now=self._clock())
            except (InvalidTransition, ConcurrentTransition):
                return CancelResult.NOT_CANCELLABLE
            self._emit_finished(controller, done, session_short=None, error_class=None)
            return CancelResult.CANCELLED
        logger.warning("cancel requested for untracked non-queued task")
        return CancelResult.NOT_CANCELLABLE

    # ---- events ----------------------------------------------------

    def _emit(
        self,
        controller: AgentController,
        name: str,
        rec: TaskRecord,
        **fields: _FieldValue,
    ) -> None:
        payload: dict[str, _FieldValue] = {
            "channel_id": rec.discord_channel_id,
            "short_id": rec.id[:8],
        }
        payload.update(fields)
        controller.emit(ControllerEvent(name, at=self._clock(), task_id=rec.id, fields=payload))

    def _emit_finished(
        self,
        controller: AgentController,
        rec: TaskRecord,
        *,
        session_short: str | None,
        error_class: str | None,
        **fields: _FieldValue,
    ) -> None:
        self._emit(
            controller,
            "task_finished",
            rec,
            status=rec.status.value,
            error_class=error_class,
            session_short=session_short,
            **fields,
        )

    # ---- the task handler -----------------------------------------

    async def handle(self, controller: AgentController, sub: TaskSubmission) -> None:
        conn = controller.conn
        assert conn is not None
        tasks = TaskRepository(conn)
        if sub.task_id is None:
            record: TaskRecord | None = tasks.create(
                project_id=sub.project_id,
                prompt=sub.prompt,
                discord_user_id=sub.discord_user_id,
                discord_guild_id=sub.discord_guild_id,
                discord_channel_id=sub.discord_channel_id,
                session_id=sub.session_id,
                now=self._clock(),
            )
        else:
            record = tasks.get(sub.task_id)
        if record is None:
            logger.warning("submitted task row not found")
            return
        if record.status != TaskStatus.QUEUED:
            return
        if self._shutting_down:
            done = tasks.transition(
                record.id,
                TaskStatus.CANCELLED,
                now=self._clock(),
                error_class=FAIL_DAEMON_SHUTDOWN,
            )
            self._emit_finished(controller, done, session_short=None, error_class=done.error_class)
            return

        cancel = asyncio.Event()
        self._cancel_event = cancel
        self._current_task_id = record.id
        self._running = RunningTask(record.id, "", None, None, 0, None, self._clock())
        try:
            await self._run_task(controller, record, cancel)
        except asyncio.CancelledError:
            try:
                tasks.transition(
                    record.id,
                    TaskStatus.CANCELLED,
                    now=self._clock(),
                    error_class=FAIL_DAEMON_SHUTDOWN,
                )
            except (InvalidTransition, ConcurrentTransition, TaskNotFound, sqlite3.Error):
                pass
            raise
        except Exception as e:
            # Outcome unknown: log the class name only; never replay and never fall back.
            logger.error("task handler failed: %s", type(e).__name__)
            self._fail_unknown(controller, record.id)
        finally:
            self._cancel_event = None
            self._current_task_id = None
            self._running = None
            self._finalize_session(conn, record.id)

    def _fail_unknown(self, controller: AgentController, task_id: str) -> None:
        """Best effort: mark the task FAILED/UNKNOWN, close open attempts, notify the user."""
        conn = controller.conn
        assert conn is not None
        unknown = ErrorClass.UNKNOWN.value
        try:
            attempts = FallbackAttemptRepository(conn)
            for att in attempts.list_for_task(task_id):
                if att.status == AttemptStatus.RUNNING:
                    attempts.finish(
                        att.id,
                        status=AttemptStatus.FAILED,
                        error_class=unknown,
                        http_status=None,
                        retry_after_ms=None,
                        now=self._clock(),
                    )
        except sqlite3.Error:
            pass
        try:
            tasks = TaskRepository(conn)
            current = tasks.get(task_id)
            session_short = (
                current.session_id[:8] if current and current.session_id is not None else None
            )
            done = tasks.transition(
                task_id, TaskStatus.FAILED, now=self._clock(), error_class=unknown
            )
            self._emit_finished(controller, done, session_short=session_short, error_class=unknown)
        except (InvalidTransition, ConcurrentTransition, TaskNotFound, sqlite3.Error):
            pass

    def _finalize_session(self, conn: sqlite3.Connection, task_id: str) -> None:
        try:
            rec = TaskRepository(conn).get(task_id)
            if rec is None or rec.status not in TERMINAL_STATUSES or rec.session_id is None:
                return
            sessions = SessionRepository(conn)
            now = self._clock()
            sessions.touch(rec.session_id, now=now)
            session = sessions.get(rec.session_id)
            if (
                session is not None
                and session.status == SessionStatus.OPEN
                and session.opencode_session_id is None
            ):
                sessions.close(session.id, now=now)
        except sqlite3.Error as e:
            logger.warning("session upkeep failed: %s", type(e).__name__)

    def _finish(
        self,
        controller: AgentController,
        task_id: str,
        status: TaskStatus,
        *,
        error_class: str | None = None,
        exit_code: int | None = None,
        session: SessionRecord | None = None,
        **fields: _FieldValue,
    ) -> TaskRecord:
        assert controller.conn is not None
        done = TaskRepository(controller.conn).transition(
            task_id, status, now=self._clock(), error_class=error_class, exit_code=exit_code
        )
        if exit_code is not None:
            fields["exit_code"] = exit_code
        self._emit_finished(
            controller,
            done,
            session_short=session.id[:8] if session is not None else None,
            error_class=error_class,
            **fields,
        )
        return done

    def _stop_class(self) -> str | None:
        return FAIL_DAEMON_SHUTDOWN if self._shutting_down else None

    async def _small_model_state(self) -> SmallModelCheck:
        """Fail-closed small_model preflight; only OK results are cached (TTL)."""
        now = self._clock()
        ttl = self._config.orchestrator.small_model_check_ttl_s
        if (
            self._small_model_ok_at is not None
            and ttl > 0
            and (now - self._small_model_ok_at).total_seconds() < ttl
        ):
            return SmallModelCheck.OK
        try:
            result = await self._executor.check_small_model(allowed_small_models(self._config))
        except Exception as e:
            logger.warning("small_model check failed: %s", type(e).__name__)
            return SmallModelCheck.UNAVAILABLE
        self._small_model_ok_at = now if result == SmallModelCheck.OK else None
        return result

    async def _maybe_refresh(self, controller: AgentController) -> None:
        assert controller.conn is not None
        now = self._clock()
        interval = self._config.orchestrator.provider_refresh_interval_s
        if (
            self._last_refresh is not None
            and interval > 0
            and (now - self._last_refresh).total_seconds() < interval
        ):
            return
        try:
            self._observations = await refresh_all(
                self._adapters, controller.conn, self._config, now=now, environ=self._environ
            )
            self._last_refresh = now
        except Exception as e:
            logger.warning("provider refresh failed: %s", type(e).__name__)

    def _progress_callback(
        self, controller: AgentController, rec: TaskRecord
    ) -> Callable[[ExecutorEvent], None]:
        interval = self._config.orchestrator.progress_min_interval_s
        counters = {"tools": 0, "steps": 0}
        last: list[datetime | None] = [None]

        def on_event(ev: ExecutorEvent) -> None:
            now = self._clock()
            if ev.kind == ExecutorEventKind.SESSION:
                if self._running is not None and ev.session_id is not None:
                    self._running = dataclasses.replace(
                        self._running, opencode_session_id=ev.session_id
                    )
                self._emit(controller, "progress", rec, opencode_session_id=ev.session_id)
                return
            if ev.kind == ExecutorEventKind.TOOL:
                counters["tools"] += 1
            else:
                counters["steps"] += 1
            if last[0] is not None and (now - last[0]).total_seconds() < interval:
                return
            last[0] = now
            self._emit(
                controller,
                "progress",
                rec,
                tool=ev.tool,
                tool_status=ev.tool_status,
                tool_events=counters["tools"],
                llm_steps=counters["steps"],
            )

        return on_event

    def _record_usage(
        self,
        conn: sqlite3.Connection,
        task_id: str,
        provider: str,
        model: str,
        result: ExecutionResult,
        ec: ErrorClass,
    ) -> None:
        if result.outcome == ExecutionOutcome.EXECUTOR_UNAVAILABLE:
            return
        if result.outcome == ExecutionOutcome.CANCELLED and result.llm_steps == 0:
            return
        count = max(result.llm_steps, 1)
        if count > _MAX_USAGE_EVENTS:
            logger.info("usage events capped at %d (steps=%d)", _MAX_USAGE_EVENTS, count)
            count = _MAX_USAGE_EVENTS
        if result.outcome == ExecutionOutcome.SUCCEEDED:
            last_status = UsageStatus.SUCCESS
        elif result.outcome == ExecutionOutcome.TIMED_OUT or (
            result.outcome == ExecutionOutcome.FAILED and ec in STOP_ERRORS
        ):
            last_status = UsageStatus.UNKNOWN_OUTCOME
        else:
            last_status = UsageStatus.ERROR
        known = result.input_tokens is not None and result.output_tokens is not None
        usage = UsageRepository(conn)
        for i in range(count):
            if i == 0:
                tin, tout = result.input_tokens, result.output_tokens
            else:
                tin, tout = (0, 0) if known else (None, None)
            usage.record(
                UsageEvent(
                    provider=provider,
                    model=model,
                    cost_class=CostClass.FREE,
                    status=last_status if i == count - 1 else UsageStatus.SUCCESS,
                    occurred_at=self._clock(),
                    task_id=task_id,
                    input_tokens=tin,
                    output_tokens=tout,
                )
            )

    async def _run_task(
        self, controller: AgentController, record: TaskRecord, cancel: asyncio.Event
    ) -> None:
        conn = controller.conn
        assert conn is not None
        cfg = self._config
        tasks = TaskRepository(conn)
        sessions = SessionRepository(conn)
        attempt_repo = FallbackAttemptRepository(conn)
        task_id = record.id

        tasks.transition(task_id, TaskStatus.ROUTING, now=self._clock())
        self._emit(controller, "task_routing", record)

        project_dir = cfg.opencode.project_directory(record.project_id)
        prompt = sanitize_prompt(record.prompt)
        if project_dir is None or prompt is None:
            self._finish(
                controller, task_id, TaskStatus.FAILED, error_class=ErrorClass.INVALID_REQUEST.value
            )
            return

        session: SessionRecord | None
        if record.session_id is not None:
            session = sessions.get(record.session_id)
            if session is None or session.status != SessionStatus.OPEN:
                self._finish(
                    controller,
                    task_id,
                    TaskStatus.FAILED,
                    error_class=ErrorClass.INVALID_REQUEST.value,
                )
                return
        else:
            session = sessions.create(project_id=record.project_id, now=self._clock())
            tasks.attach_session(task_id, session.id, now=self._clock())
        oc_sid = session.opencode_session_id
        if self._running is not None:
            self._running = dataclasses.replace(
                self._running, session_id=session.id, opencode_session_id=oc_sid
            )

        try:
            health = await self._executor.health_check()
            healthy, detail = health.healthy, health.detail
        except Exception as e:
            healthy, detail = False, type(e).__name__
        if not healthy:
            self._finish(
                controller,
                task_id,
                TaskStatus.FAILED,
                error_class=FAIL_OPENCODE_UNAVAILABLE,
                session=session,
                detail=detail,
            )
            return

        sm = await self._small_model_state()
        if sm != SmallModelCheck.OK:
            unsafe = sm == SmallModelCheck.UNSAFE
            self._finish(
                controller,
                task_id,
                TaskStatus.FAILED,
                error_class=FAIL_OPENCODE_UNSAFE_CONFIG if unsafe else FAIL_OPENCODE_UNAVAILABLE,
                session=session,
                detail="small_model not allowlisted" if unsafe else "GET /config failed",
            )
            return

        # Per-task on-disk project config check (never cached across projects).
        try:
            pc, pc_detail = await self._executor.check_project_models(
                allowed_small_models(cfg), project_dir
            )
        except Exception as e:
            logger.warning("project config check failed: %s", type(e).__name__)
            pc, pc_detail = SmallModelCheck.UNSAFE, "project config check failed"
        if pc != SmallModelCheck.OK:
            self._finish(
                controller,
                task_id,
                TaskStatus.FAILED,
                error_class=FAIL_OPENCODE_UNSAFE_CONFIG,
                session=session,
                detail="project config not allowlisted",
                config_detail=pc_detail,
            )
            return

        await self._maybe_refresh(controller)

        request = RouteRequest(
            task_id,
            record.project_id,
            RequiredCapabilities(tool_calling=cfg.orchestrator.require_tool_calling),
            session_id=session.id,
        )

        def state_factory() -> RouterState:
            return build_router_state(
                cfg,
                conn,
                self._cooldowns,
                self._observations,
                now=self._clock(),
                environ=self._environ,
            )

        attempts: list[FailedAttempt] = []
        prompt_to_send = prompt
        send_session = oc_sid

        while True:
            if cancel.is_set():
                self._finish(
                    controller,
                    task_id,
                    TaskStatus.CANCELLED,
                    error_class=self._stop_class(),
                    session=session,
                )
                return

            decision = route_with_state(request, cfg, state_factory, attempts=attempts)
            if isinstance(decision, NoEligibleProvider):
                if decision.reason == NoEligibleReason.NON_FALLBACK_ERROR and attempts:
                    ec_str = attempts[-1].error_class.value
                else:
                    ec_str = decision.reason.value
                self._finish(
                    controller,
                    task_id,
                    TaskStatus.FAILED,
                    error_class=ec_str,
                    session=session,
                    reason=decision.reason.value,
                    attempts=len(attempts),
                )
                return
            assert isinstance(decision, Selected)

            cand = decision.candidate
            provider, model = cand.provider, cand.model
            tasks.set_selection(task_id, provider=provider, model=model, now=self._clock())
            sessions.touch(session.id, now=self._clock())
            tasks.transition(task_id, TaskStatus.RUNNING, now=self._clock())
            attempt_no = len(attempts) + 1
            attempt_id = attempt_repo.start(
                task_id=task_id,
                session_id=session.id,
                attempt_no=attempt_no,
                provider=provider,
                model=model,
                now=self._clock(),
            )
            if self._running is not None:
                self._running = dataclasses.replace(
                    self._running,
                    provider=provider,
                    model=model,
                    attempt_no=attempt_no,
                    opencode_session_id=oc_sid,
                )
            skipped = ",".join(
                f"{s.candidate.provider}/{s.candidate.model}:{s.reason.value}"
                for s in decision.skipped
            )[:300]
            self._emit(
                controller,
                "provider_selected",
                record,
                provider=provider,
                model=model,
                attempt_no=attempt_no,
                policy_id=cand.policy_id,
                skipped=skipped,
            )

            result = await self._executor.run(
                task_id=task_id,
                provider=provider,
                model=model,
                prompt=prompt_to_send,
                session_id=send_session,
                project_dir=project_dir,
                cancel_event=cancel,
                on_event=self._progress_callback(controller, record),
            )

            if result.session_id is not None and session.opencode_session_id is None:
                session = sessions.set_opencode_session_id(
                    session.id, result.session_id, now=self._clock()
                )
                oc_sid = session.opencode_session_id
                if self._running is not None:
                    self._running = dataclasses.replace(self._running, opencode_session_id=oc_sid)

            ec = result.error_class or ErrorClass.UNKNOWN
            self._record_usage(conn, task_id, provider, model, result, ec)

            retry_ms = (
                round(result.retry_after_s * 1000) if result.retry_after_s is not None else None
            )
            attempt_repo.finish(
                attempt_id,
                status=_ATTEMPT_STATUS[result.outcome],
                error_class=result.error_class.value if result.error_class else None,
                http_status=result.http_status,
                retry_after_ms=retry_ms,
                now=self._clock(),
            )

            if result.outcome == ExecutionOutcome.SUCCEEDED:
                self._cooldowns.record_success(provider)
                self._finish(
                    controller,
                    task_id,
                    TaskStatus.SUCCEEDED,
                    exit_code=result.exit_code,
                    session=session,
                    provider=provider,
                    model=model,
                    attempts=attempt_no,
                    changed_files=",".join(result.changed_files)[:500],
                    output_tail=result.output_tail[-1500:],
                    session_id=oc_sid,
                )
                return
            if result.outcome == ExecutionOutcome.CANCELLED:
                self._finish(
                    controller,
                    task_id,
                    TaskStatus.CANCELLED,
                    error_class=self._stop_class(),
                    session=session,
                )
                return
            if result.outcome == ExecutionOutcome.EXECUTOR_UNAVAILABLE:
                self._finish(
                    controller,
                    task_id,
                    TaskStatus.FAILED,
                    error_class=FAIL_OPENCODE_UNAVAILABLE,
                    session=session,
                    detail=result.detail,
                )
                return

            # FAILED / TIMED_OUT
            self._cooldowns.record_failure(
                provider, model, ec, retry_after_s=result.retry_after_s, now=self._clock()
            )
            if result.session_id is None and oc_sid is None and result.tool_events > 0:
                ec = ErrorClass.UNKNOWN
            attempts.append(FailedAttempt(provider, model, ec))
            self._emit(
                controller,
                "attempt_failed",
                record,
                provider=provider,
                model=model,
                attempt_no=attempt_no,
                error_class=ec.value,
                http_status=result.http_status,
                retry_after_ms=retry_ms,
            )
            tasks.transition(task_id, TaskStatus.ROUTING, now=self._clock())
            if oc_sid is not None:
                prompt_to_send = cfg.opencode.continue_prompt
                send_session = oc_sid
            else:
                send_session = None
