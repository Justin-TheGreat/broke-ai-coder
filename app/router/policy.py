from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum

from app.config.models import AppConfig


class ModelRoutingStatus(StrEnum):
    ALLOWED = "ALLOWED"
    DISCOVERED_ONLY = "DISCOVERED_ONLY"


@dataclass(frozen=True, slots=True)
class ModelListing:
    provider: str
    model: str
    status: ModelRoutingStatus
    policy_id: str | None
    rank: int | None


def is_allowlisted(config: AppConfig, provider: str, model: str) -> bool:
    return any(
        pol.provider == provider and model in pol.model_order
        for pol in config.routing.policies.values()
    )


def model_listing(
    config: AppConfig, provider: str, discovered: Iterable[str]
) -> list[ModelListing]:
    routing = config.routing
    ordered_ids = list(routing.provider_order) + [
        pid for pid in routing.policies if pid not in routing.provider_order
    ]
    out: list[ModelListing] = []
    allowed: set[str] = set()
    for pid in ordered_ids:
        pol = routing.policies[pid]
        if pol.provider != provider:
            continue
        for rank, model in enumerate(pol.model_order):
            out.append(ModelListing(provider, model, ModelRoutingStatus.ALLOWED, pid, rank))
            allowed.add(model)
    for model in sorted(set(discovered) - allowed):
        out.append(ModelListing(provider, model, ModelRoutingStatus.DISCOVERED_ONLY, None, None))
    return out
