from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from app.providers.base import ErrorClass

if TYPE_CHECKING:
    from app.runtime.daemon import AgentController

logger = logging.getLogger(__name__)


class ChatFrontend(Protocol):
    """Future Discord bot."""

    async def start(self, controller: AgentController) -> None: ...

    async def stop(self) -> None: ...


class NullFrontend:
    async def start(self, controller: AgentController) -> None:
        logger.info("frontend disabled")

    async def stop(self) -> None:
        return None


@dataclass(frozen=True, slots=True)
class ExecutionResult:
    exit_code: int | None
    error_class: ErrorClass | None


class AgentExecutor(Protocol):
    """Future OpenCode executor; not wired in this slice."""

    async def run(
        self,
        *,
        task_id: str,
        provider: str,
        model: str,
        prompt: str,
        session_id: str | None,
    ) -> ExecutionResult: ...
