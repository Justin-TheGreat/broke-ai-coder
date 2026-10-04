from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass

from app.config.models import AppConfig
from app.opencode.executor import OpenCodeExecutor
from app.orchestrator.orchestrator import Orchestrator
from app.providers.http import HttpProviderAdapter
from app.providers.registry import build_adapters
from app.redaction import SecretRedactor
from app.runtime.daemon import AgentController
from app.runtime.interfaces import ChatFrontend, NullFrontend

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class Runtime:
    controller: AgentController
    orchestrator: Orchestrator
    executor: OpenCodeExecutor
    adapters: dict[str, HttpProviderAdapter]


def build_runtime(
    config: AppConfig,
    *,
    redactor: SecretRedactor,
    environ: Mapping[str, str] | None = None,
    once: bool = False,
) -> Runtime:
    adapters = build_adapters(config, environ=environ, redactor=redactor)
    executor = OpenCodeExecutor(
        config.opencode, redactor=redactor, bot_token_env=config.discord.bot_token_env
    )
    orchestrator = Orchestrator(config, executor=executor, adapters=adapters, environ=environ)
    frontend: ChatFrontend
    if once:
        frontend = NullFrontend()
    else:
        # Imported lazily so --check and --once never import discord.
        from app.bot.discord_frontend import create_frontend

        frontend = create_frontend(config, redactor=redactor, environ=environ)
    controller = AgentController(config, frontend=frontend, orchestrator=orchestrator)
    return Runtime(controller, orchestrator, executor, adapters)


async def close_runtime(rt: Runtime) -> None:
    for adapter in rt.adapters.values():
        try:
            await adapter.aclose()
        except Exception as e:
            logger.warning("adapter close failed: %s", type(e).__name__)
    try:
        await rt.executor.aclose()
    except Exception as e:
        logger.warning("executor close failed: %s", type(e).__name__)
