from __future__ import annotations

import asyncio
import logging
import sqlite3
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime

from app.config.models import AppConfig
from app.config.secrets import credentials_present
from app.db.provider_models import ProviderModelRepository
from app.db.quota import QuotaSnapshotRepository
from app.db.usage import UsageRepository
from app.providers.base import (
    HealthStatus,
    ModelCapability,
    ProviderAdapter,
    ProviderHealth,
)
from app.providers.errors import ProviderError
from app.quota.cooldown import CooldownManager
from app.quota.estimator import estimate_quota
from app.quota.normalize import ESTIMATE_SOURCE_PREFIX
from app.router.policy import is_allowlisted
from app.router.router import next_after_failure, route
from app.router.types import (
    FailedAttempt,
    NoEligibleProvider,
    NoEligibleReason,
    RouteDecision,
    RouteRequest,
    RouterState,
)

logger = logging.getLogger(__name__)


class RouterStateError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class ProviderObservation:
    provider: str
    health: ProviderHealth
    models: tuple[ModelCapability, ...]
    models_ok: bool
    observed_at: datetime


def _describe(error: Exception) -> str:
    if isinstance(error, ProviderError):
        return str(error.error_class)
    return type(error).__name__


async def refresh_provider(
    adapter: ProviderAdapter, conn: sqlite3.Connection, config: AppConfig, *, now: datetime
) -> ProviderObservation:
    provider = adapter.provider_id
    try:
        health = await adapter.health_check()
    except Exception as e:
        health = ProviderHealth(provider, HealthStatus.DOWN, now, f"error: {type(e).__name__}")

    models: tuple[ModelCapability, ...] = ()
    models_ok = False
    try:
        discovered = await adapter.list_models()
        ProviderModelRepository(conn).replace_discovered(config, provider, discovered, now)
        models = tuple(discovered)
        models_ok = True
    except Exception as e:
        try:
            cls = str(await adapter.classify_error(e))
        except Exception:
            cls = type(e).__name__
        logger.warning("provider=%s list_models failed: %s", provider, cls)

    try:
        records = await adapter.get_quota()
        QuotaSnapshotRepository(conn).insert_many(records)
    except Exception as e:
        logger.warning("provider=%s get_quota failed: %s", provider, _describe(e))

    return ProviderObservation(provider, health, models, models_ok, now)


async def refresh_all(
    adapters: Mapping[str, ProviderAdapter],
    conn: sqlite3.Connection,
    config: AppConfig,
    *,
    now: datetime,
    environ: Mapping[str, str] | None = None,
) -> dict[str, ProviderObservation]:
    present = credentials_present(config, environ)
    names = [
        name
        for name, prov in config.providers.items()
        if prov.enabled and name in present and name in adapters
    ]
    results = await asyncio.gather(
        *(refresh_provider(adapters[n], conn, config, now=now) for n in names)
    )
    return dict(zip(names, results, strict=True))


def build_router_state(
    config: AppConfig,
    conn: sqlite3.Connection,
    cooldowns: CooldownManager,
    observations: Mapping[str, ProviderObservation],
    *,
    now: datetime,
    environ: Mapping[str, str] | None = None,
) -> RouterState:
    try:
        return _build(config, conn, cooldowns, observations, now, environ)
    except RouterStateError:
        raise
    except (ValueError, sqlite3.Error, TypeError) as e:
        raise RouterStateError(f"cannot build router state: {e}") from e


def _build(
    config: AppConfig,
    conn: sqlite3.Connection,
    cooldowns: CooldownManager,
    observations: Mapping[str, ProviderObservation],
    now: datetime,
    environ: Mapping[str, str] | None,
) -> RouterState:
    models_repo = ProviderModelRepository(conn)
    capabilities: dict[tuple[str, str], ModelCapability] = {}
    for provider in config.providers:
        obs = observations.get(provider)
        caps = (
            obs.models
            if obs is not None and obs.models_ok
            else models_repo.allowed_capabilities(config, provider)
        )
        for cap in caps:
            if is_allowlisted(config, provider, cap.model):
                capabilities[(provider, cap.model)] = cap

    exact = [
        r
        for r in QuotaSnapshotRepository(conn).latest(now)
        if not r.source.startswith(ESTIMATE_SOURCE_PREFIX)
    ]
    usage = UsageRepository(conn)
    quota = exact + estimate_quota(config, usage, now, exact=exact)

    return RouterState(
        now=now,
        credentials_present=credentials_present(config, environ),
        capabilities=capabilities,
        quota=quota,
        health={p: obs.health.status for p, obs in observations.items()},
        cooldowns=cooldowns.active(now),
    )


def route_with_state(
    request: RouteRequest,
    config: AppConfig,
    state_factory: Callable[[], RouterState],
    *,
    attempts: Sequence[FailedAttempt] = (),
) -> RouteDecision:
    try:
        state = state_factory()
    except RouterStateError as e:
        logger.error("router state unavailable: %s", e)
        return NoEligibleProvider(NoEligibleReason.ROUTER_STATE_UNAVAILABLE, ())
    if not attempts:
        return route(request, config, state)
    return next_after_failure(request, attempts, config, state)
