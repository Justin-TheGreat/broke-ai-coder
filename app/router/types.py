from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum

from app.providers.base import (
    ErrorClass,
    HealthStatus,
    ModelCapability,
    QuotaRecord,
)


@dataclass(frozen=True, slots=True)
class RequiredCapabilities:
    tool_calling: bool = False
    structured_output: bool = False
    vision: bool = False
    large_context: bool = False


@dataclass(frozen=True, slots=True)
class RouteRequest:
    task_id: str
    project_id: str
    required_capabilities: RequiredCapabilities = RequiredCapabilities()
    estimated_input_tokens: int = 0
    estimated_output_tokens: int = 0
    session_id: str | None = None

    def __post_init__(self) -> None:
        if self.estimated_input_tokens < 0:
            raise ValueError("estimated_input_tokens must be >= 0")
        if self.estimated_output_tokens < 0:
            raise ValueError("estimated_output_tokens must be >= 0")


@dataclass(frozen=True, slots=True)
class RouterState:
    now: datetime
    credentials_present: frozenset[str] = frozenset()
    capabilities: Mapping[tuple[str, str], ModelCapability] = field(default_factory=dict)
    quota: Sequence[QuotaRecord] = ()
    health: Mapping[str, HealthStatus] = field(default_factory=dict)
    cooldowns: Mapping[tuple[str, str | None], datetime] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class Candidate:
    policy_id: str
    provider: str
    model: str
    provider_rank: int
    model_rank: int

    @property
    def sort_key(self) -> tuple[int, int]:
        return (self.provider_rank, self.model_rank)


class SkipReason(StrEnum):
    EXCLUDED_AFTER_FAILURE = "EXCLUDED_AFTER_FAILURE"
    POLICY_DISABLED = "POLICY_DISABLED"
    PROVIDER_DISABLED = "PROVIDER_DISABLED"
    MISSING_CREDENTIAL = "MISSING_CREDENTIAL"
    PROVIDER_DOWN = "PROVIDER_DOWN"
    COOLDOWN = "COOLDOWN"
    MODEL_UNKNOWN = "MODEL_UNKNOWN"
    CAPABILITY_MISMATCH = "CAPABILITY_MISMATCH"
    CONTEXT_TOO_SMALL = "CONTEXT_TOO_SMALL"
    QUOTA_EXHAUSTED = "QUOTA_EXHAUSTED"
    QUOTA_INSUFFICIENT = "QUOTA_INSUFFICIENT"


@dataclass(frozen=True, slots=True)
class Skipped:
    candidate: Candidate
    reason: SkipReason


class NoEligibleReason(StrEnum):
    FREE_CAPACITY_EXHAUSTED = "FREE_CAPACITY_EXHAUSTED"
    MAX_FALLBACK_ATTEMPTS_REACHED = "MAX_FALLBACK_ATTEMPTS_REACHED"
    NON_FALLBACK_ERROR = "NON_FALLBACK_ERROR"
    ROUTER_STATE_UNAVAILABLE = "ROUTER_STATE_UNAVAILABLE"


@dataclass(frozen=True, slots=True)
class Selected:
    candidate: Candidate
    skipped: tuple[Skipped, ...]


@dataclass(frozen=True, slots=True)
class NoEligibleProvider:
    reason: NoEligibleReason
    skipped: tuple[Skipped, ...]


RouteDecision = Selected | NoEligibleProvider


@dataclass(frozen=True, slots=True)
class FailedAttempt:
    provider: str
    model: str
    error_class: ErrorClass
