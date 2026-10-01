from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Protocol

from app.providers.base import ErrorClass

if TYPE_CHECKING:
    from app.runtime.daemon import AgentController, ControllerEvent

logger = logging.getLogger(__name__)


class ChatFrontend(Protocol):
    """Chat frontend (Discord)."""

    async def start(self, controller: AgentController) -> None: ...

    async def stop(self) -> None: ...

    async def on_event(self, event: ControllerEvent) -> None: ...


class NullFrontend:
    async def start(self, controller: AgentController) -> None:
        logger.info("frontend disabled")

    async def stop(self) -> None:
        return None

    async def on_event(self, event: ControllerEvent) -> None:
        return None


class ExecutionOutcome(StrEnum):
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    TIMED_OUT = "TIMED_OUT"
    CANCELLED = "CANCELLED"
    EXECUTOR_UNAVAILABLE = "EXECUTOR_UNAVAILABLE"


class ExecutorEventKind(StrEnum):
    SESSION = "SESSION"  # session id first seen
    TOOL = "TOOL"  # tool_use event
    STEP = "STEP"  # step_finish


@dataclass(frozen=True, slots=True)
class ExecutorEvent:
    kind: ExecutorEventKind
    session_id: str | None = None
    tool: str | None = None  # truncated to 40 chars
    tool_status: str | None = None  # truncated to 20 chars


@dataclass(frozen=True, slots=True)
class ExecutionResult:
    outcome: ExecutionOutcome
    exit_code: int | None = None
    error_class: ErrorClass | None = None  # None for SUCCEEDED/CANCELLED/EXECUTOR_UNAVAILABLE
    session_id: str | None = None
    http_status: int | None = None
    retry_after_s: float | None = None
    error_message: str | None = None  # redacted, <= 300 chars
    output_tail: str = ""  # redacted, <= output_tail_chars
    output_truncated: bool = False
    tool_events: int = 0
    llm_steps: int = 0
    input_tokens: int | None = None  # None unless every step_finish carried ints
    output_tokens: int | None = None
    changed_files: tuple[str, ...] = ()  # <= 20 unique, each <= 200 chars, first-seen order
    detail: str | None = None  # short infra detail for EXECUTOR_UNAVAILABLE, no secrets


@dataclass(frozen=True, slots=True)
class ExecutorHealth:
    healthy: bool
    version: str | None
    detail: str


class AgentExecutor(Protocol):
    async def health_check(self) -> ExecutorHealth: ...

    async def run(
        self,
        *,
        task_id: str,
        provider: str,
        model: str,
        prompt: str,
        session_id: str | None,
        project_dir: str,
        cancel_event: asyncio.Event,
        on_event: Callable[[ExecutorEvent], None] | None = None,
    ) -> ExecutionResult: ...

    async def aclose(self) -> None: ...
