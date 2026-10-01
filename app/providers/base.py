from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Protocol, runtime_checkable


class CostClass(StrEnum):
    FREE = "FREE"
    PAID = "PAID"


class QuotaConfidence(StrEnum):
    EXACT = "EXACT"
    ESTIMATED = "ESTIMATED"
    UNKNOWN = "UNKNOWN"
    EXHAUSTED = "EXHAUSTED"
    COOLDOWN = "COOLDOWN"


class QuotaUnit(StrEnum):
    REQUESTS = "REQUESTS"
    TOKENS = "TOKENS"
    USD = "USD"


class HealthStatus(StrEnum):
    HEALTHY = "HEALTHY"
    DEGRADED = "DEGRADED"
    DOWN = "DOWN"


class ErrorClass(StrEnum):
    RATE_LIMITED = "RATE_LIMITED"
    QUOTA_EXHAUSTED = "QUOTA_EXHAUSTED"
    MODEL_UNAVAILABLE = "MODEL_UNAVAILABLE"
    CONTEXT_TOO_LARGE = "CONTEXT_TOO_LARGE"
    CAPABILITY_UNSUPPORTED = "CAPABILITY_UNSUPPORTED"
    TRANSIENT_NETWORK = "TRANSIENT_NETWORK"
    PROVIDER_UNAVAILABLE = "PROVIDER_UNAVAILABLE"
    AUTH_FAILED = "AUTH_FAILED"
    TIMEOUT_UNKNOWN_OUTCOME = "TIMEOUT_UNKNOWN_OUTCOME"
    INVALID_REQUEST = "INVALID_REQUEST"
    POLICY_REJECTED = "POLICY_REJECTED"
    UNKNOWN = "UNKNOWN"


PAIR_FALLBACK_ERRORS: frozenset[ErrorClass] = frozenset(
    {
        ErrorClass.RATE_LIMITED,
        ErrorClass.QUOTA_EXHAUSTED,
        ErrorClass.MODEL_UNAVAILABLE,
        ErrorClass.CONTEXT_TOO_LARGE,
        ErrorClass.CAPABILITY_UNSUPPORTED,
        ErrorClass.TRANSIENT_NETWORK,
    }
)
PROVIDER_FALLBACK_ERRORS: frozenset[ErrorClass] = frozenset(
    {ErrorClass.PROVIDER_UNAVAILABLE, ErrorClass.AUTH_FAILED}
)
STOP_ERRORS: frozenset[ErrorClass] = frozenset(
    {
        ErrorClass.TIMEOUT_UNKNOWN_OUTCOME,
        ErrorClass.INVALID_REQUEST,
        ErrorClass.POLICY_REJECTED,
        ErrorClass.UNKNOWN,
    }
)


@dataclass(frozen=True, slots=True)
class ModelCapability:
    provider: str
    model: str
    supports_tool_calling: bool = False
    supports_structured_output: bool = False
    supports_vision: bool = False
    context_window: int | None = None
    max_output_tokens: int | None = None


@dataclass(frozen=True, slots=True)
class QuotaRecord:
    provider: str
    model: str | None
    window: str
    unit: QuotaUnit
    confidence: QuotaConfidence
    observed_at: datetime
    source: str
    limit: int | float | None = None
    used: int | float | None = None
    remaining: int | float | None = None
    reset_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class ProviderHealth:
    provider: str
    status: HealthStatus
    checked_at: datetime
    detail: str | None = None


@runtime_checkable
class ProviderAdapter(Protocol):
    provider_id: str

    async def health_check(self) -> ProviderHealth: ...

    async def get_quota(self) -> list[QuotaRecord]: ...

    async def list_models(self) -> list[ModelCapability]: ...

    async def classify_error(self, error: Exception) -> ErrorClass: ...
