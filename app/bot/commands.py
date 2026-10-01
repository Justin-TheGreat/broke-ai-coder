"""Pure command handlers; no discord import so they are testable offline."""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Protocol

from app.bot import format as fmt
from app.bot.auth import authorize
from app.config.models import DiscordConfig
from app.db.sessions import SessionRecord
from app.db.tasks import TaskRecord
from app.db.usage import UsageTotals
from app.orchestrator.orchestrator import CancelResult, RunningTask
from app.providers.base import ModelCapability, QuotaRecord
from app.redaction import SecretRedactor
from app.router.policy import ModelListing
from app.runtime.interfaces import ExecutorHealth

logger = logging.getLogger(__name__)

COMMAND_NAMES = ("code", "status", "usage", "models", "sessions", "cancel")
REF_RE = re.compile(r"^[0-9a-f-]{6,36}$")


@dataclass(frozen=True, slots=True)
class CommandContext:
    user_id: int
    guild_id: int | None
    channel_id: int | None


@dataclass(frozen=True, slots=True)
class Reply:
    content: str
    ephemeral: bool


@dataclass(frozen=True, slots=True)
class StatusSnapshot:
    opencode: ExecutorHealth
    running: RunningTask | None
    queue_depth: int
    recent: tuple[TaskRecord, ...]  # <= 5


@dataclass(frozen=True, slots=True)
class UsageSnapshot:
    quota: tuple[QuotaRecord, ...]
    cooldowns: Mapping[tuple[str, str | None], datetime]
    today: tuple[tuple[str, str, UsageTotals], ...]
    now: datetime


@dataclass(frozen=True, slots=True)
class ProviderModels:
    policy_id: str
    provider: str
    listings: tuple[ModelListing, ...]
    capabilities: Mapping[str, ModelCapability]


class SubmitStatus(StrEnum):
    QUEUED = "QUEUED"
    QUEUE_FULL = "QUEUE_FULL"
    INVALID_PROMPT = "INVALID_PROMPT"
    UNKNOWN_PROJECT = "UNKNOWN_PROJECT"
    UNKNOWN_SESSION = "UNKNOWN_SESSION"
    AMBIGUOUS_SESSION = "AMBIGUOUS_SESSION"
    SESSION_CLOSED = "SESSION_CLOSED"


@dataclass(frozen=True, slots=True)
class SubmitResult:
    status: SubmitStatus
    record: TaskRecord | None = None
    queue_position: int = 0


class BotBackend(Protocol):
    def submit_code(
        self, *, prompt: str, project: str | None, session_ref: str | None, ctx: CommandContext
    ) -> SubmitResult: ...

    def cancel(self, task_ref: str) -> tuple[CancelResult | None, str]: ...

    async def status(self) -> StatusSnapshot: ...

    def usage(self) -> UsageSnapshot: ...

    def models(self) -> list[ProviderModels]: ...

    def sessions(self, limit: int = 10) -> list[SessionRecord]: ...


_SUBMIT_MESSAGES = {
    SubmitStatus.QUEUE_FULL: "The task queue is full; try again shortly.",
    SubmitStatus.INVALID_PROMPT: "Invalid prompt (empty or longer than 4000 characters).",
    SubmitStatus.UNKNOWN_PROJECT: "Unknown project.",
    SubmitStatus.UNKNOWN_SESSION: "Unknown session.",
    SubmitStatus.AMBIGUOUS_SESSION: "ambiguous id: use more characters of the session id.",
    SubmitStatus.SESSION_CLOSED: "That session is closed; start a new one without `session`.",
}


async def run_command(
    name: str,
    *,
    cfg: DiscordConfig,
    backend: BotBackend,
    ctx: CommandContext,
    redactor: SecretRedactor | None,
    options: Mapping[str, str | None],
) -> Reply:
    decision = authorize(cfg, user_id=ctx.user_id, guild_id=ctx.guild_id, channel_id=ctx.channel_id)
    if not decision.allowed:
        logger.warning(
            "discord auth denied command=%s user=%s reason=%s", name, ctx.user_id, decision.reason
        )
        return Reply("Not authorized.", ephemeral=True)
    if name not in COMMAND_NAMES:
        return Reply("Unknown command.", ephemeral=True)
    try:
        return await _dispatch(name, backend, ctx, redactor, options)
    except Exception as e:
        logger.warning("discord command failed command=%s error=%s", name, type(e).__name__)
        return Reply(fmt.safe_text(f"Internal error ({type(e).__name__}).", redactor), True)


async def _dispatch(
    name: str,
    backend: BotBackend,
    ctx: CommandContext,
    redactor: SecretRedactor | None,
    options: Mapping[str, str | None],
) -> Reply:
    if name == "code":
        session_ref = options.get("session")
        if session_ref is not None:
            session_ref = session_ref.strip().lower()
            if not REF_RE.match(session_ref):
                return Reply(_SUBMIT_MESSAGES[SubmitStatus.UNKNOWN_SESSION], False)
        result = backend.submit_code(
            prompt=options.get("prompt") or "",
            project=options.get("project") or "default",
            session_ref=session_ref,
            ctx=ctx,
        )
        if result.status == SubmitStatus.QUEUED and result.record is not None:
            return Reply(
                fmt.format_ack(result.record, result.queue_position, redactor=redactor), False
            )
        return Reply(
            fmt.safe_text(
                _SUBMIT_MESSAGES.get(result.status, "Could not queue the task."), redactor
            ),
            False,
        )
    if name == "cancel":
        ref = (options.get("task") or "").strip().lower()
        if not REF_RE.match(ref):
            return Reply("Invalid task id.", True)
        outcome, message = backend.cancel(ref)
        if outcome is None:
            return Reply(fmt.safe_text(message, redactor), True)
        return Reply(fmt.format_cancel(outcome, ref, redactor=redactor), True)
    if name == "status":
        return Reply(fmt.format_status(await backend.status(), redactor=redactor), True)
    if name == "usage":
        return Reply(fmt.format_usage(backend.usage(), redactor=redactor), True)
    if name == "models":
        return Reply(fmt.format_models(backend.models(), redactor=redactor), True)
    if name == "sessions":
        return Reply(fmt.format_sessions(backend.sessions(), redactor=redactor), True)
    return Reply("Unknown command.", True)
