from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from app.runtime.interfaces import (
    ExecutionOutcome,
    ExecutionResult,
    ExecutorEvent,
    ExecutorHealth,
    SmallModelCheck,
)

HEALTHY = ExecutorHealth(True, "0.0-test", "ok")

Item = ExecutionResult | Callable[..., Awaitable[ExecutionResult]]


class ScriptedExecutor:
    def __init__(
        self,
        results: list[Item],
        health: ExecutorHealth = HEALTHY,
        server_check: SmallModelCheck = SmallModelCheck.OK,
        project_check: SmallModelCheck = SmallModelCheck.OK,
    ) -> None:
        self.project_check = project_check
        self.project_calls: list[str] = []
        self.server_check = server_check
        self.server_check_calls: list[frozenset[str]] = []
        self._results = list(results)
        self._health = health
        self.calls: list[dict[str, Any]] = []
        self.health_calls = 0

    async def health_check(self) -> ExecutorHealth:
        self.health_calls += 1
        return self._health

    async def check_server_config(self, allowed: frozenset[str]) -> tuple[SmallModelCheck, str]:
        self.server_check_calls.append(allowed)
        return self.server_check, "provider.broke.models"

    async def check_project_models(
        self, allowed: frozenset[str], project_dir: str
    ) -> tuple[SmallModelCheck, str]:
        self.project_calls.append(project_dir)
        return self.project_check, "fake.json: key 'model' is not an allowlisted model"

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
    ) -> ExecutionResult:
        kwargs = {
            "task_id": task_id,
            "provider": provider,
            "model": model,
            "prompt": prompt,
            "session_id": session_id,
            "project_dir": project_dir,
            "cancel_event": cancel_event,
            "on_event": on_event,
        }
        self.calls.append(kwargs)
        item = self._results[min(len(self.calls) - 1, len(self._results) - 1)]
        if isinstance(item, ExecutionResult):
            return item
        return await item(**kwargs)

    async def aclose(self) -> None:
        return None


def ok(**kw: Any) -> ExecutionResult:
    return ExecutionResult(outcome=ExecutionOutcome.SUCCEEDED, exit_code=0, **kw)
