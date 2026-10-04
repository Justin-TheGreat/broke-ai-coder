from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from app.config.models import AppConfig
from app.db.usage import UsageEvent, UsageRepository
from app.gateway.server import LlmGateway
from app.opencode.endpoint import ServerEndpoint
from app.opencode.executor import OpenCodeExecutor
from app.opencode.server import OpenCodeServerManager
from app.orchestrator.orchestrator import Orchestrator
from app.providers.http import HttpProviderAdapter
from app.providers.registry import build_adapters
from app.quota.cooldown import CooldownManager
from app.redaction import SecretRedactor
from app.runtime.daemon import AgentController
from app.runtime.interfaces import ChatFrontend, NullFrontend, RuntimeService

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class Runtime:
    controller: AgentController
    orchestrator: Orchestrator
    executor: OpenCodeExecutor
    adapters: dict[str, HttpProviderAdapter]
    gateway: LlmGateway
    server_manager: OpenCodeServerManager | None


def build_runtime(
    config: AppConfig,
    *,
    redactor: SecretRedactor,
    environ: Mapping[str, str] | None = None,
    once: bool = False,
) -> Runtime:
    adapters = build_adapters(config, environ=environ, redactor=redactor)
    cooldowns = CooldownManager(config.cooldown)
    holder: dict[str, AgentController] = {}
    orch_holder: dict[str, Orchestrator] = {}

    def _sink(event: UsageEvent) -> None:
        conn = holder["controller"].conn if "controller" in holder else None
        if conn is not None:
            UsageRepository(conn).record(event)

    def _current_task_id() -> str | None:
        orchestrator = orch_holder.get("orchestrator")
        running = orchestrator.running if orchestrator is not None else None
        return running.task_id if running is not None else None

    gateway = LlmGateway(
        config,
        adapters=adapters,
        cooldowns=cooldowns,
        usage_sink=_sink,
        current_task_id=_current_task_id,
        redactor=redactor,
    )
    redactor.add(gateway.token)

    manager: OpenCodeServerManager | None = None
    endpoint: Callable[[], ServerEndpoint]
    if config.opencode.managed_server:
        manager = OpenCodeServerManager(config, gateway=gateway, redactor=redactor, environ=environ)
        redactor.add(manager.password)
        managed = manager

        def endpoint() -> ServerEndpoint:
            return managed.endpoint

    else:
        static = ServerEndpoint(config.opencode.server_url)

        def endpoint() -> ServerEndpoint:
            return static

    executor = OpenCodeExecutor(
        config.opencode,
        redactor=redactor,
        endpoint=endpoint,
        gateway_base_url=lambda: gateway.base_url,
        environ=environ,
    )
    orchestrator = Orchestrator(
        config, executor=executor, adapters=adapters, environ=environ, cooldowns=cooldowns
    )
    orch_holder["orchestrator"] = orchestrator
    frontend: ChatFrontend
    if once:
        frontend = NullFrontend()
    else:
        # Imported lazily so --check and --once never import discord.
        from app.bot.discord_frontend import create_frontend

        frontend = create_frontend(config, redactor=redactor, environ=environ)
    services: list[RuntimeService] = [gateway]
    if manager is not None:
        services.append(manager)
    controller = AgentController(
        config, frontend=frontend, orchestrator=orchestrator, services=services
    )
    holder["controller"] = controller
    return Runtime(controller, orchestrator, executor, adapters, gateway, manager)


async def close_runtime(rt: Runtime) -> None:
    try:
        await rt.gateway.stop()
    except Exception as e:
        logger.warning("gateway close failed: %s", type(e).__name__)
    for adapter in rt.adapters.values():
        try:
            await adapter.aclose()
        except Exception as e:
            logger.warning("adapter close failed: %s", type(e).__name__)
    try:
        await rt.executor.aclose()
    except Exception as e:
        logger.warning("executor close failed: %s", type(e).__name__)
