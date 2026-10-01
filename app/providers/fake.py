from __future__ import annotations

from collections.abc import Sequence

from app.providers.base import (
    ErrorClass,
    HealthStatus,
    ModelCapability,
    ProviderHealth,
    QuotaRecord,
)
from app.timeutil import utcnow


class FakeProviderError(Exception):
    def __init__(self, error_class: ErrorClass, message: str = "") -> None:
        super().__init__(message or error_class.value)
        self.error_class = error_class


class FakeProviderAdapter:
    def __init__(
        self,
        provider_id: str,
        *,
        models: Sequence[ModelCapability] = (),
        quota: Sequence[QuotaRecord] = (),
        health: ProviderHealth | None = None,
    ) -> None:
        self.provider_id = provider_id
        self._models = list(models)
        self._quota = list(quota)
        self._health = health or ProviderHealth(provider_id, HealthStatus.HEALTHY, utcnow())
        self.calls: list[str] = []

    def set_models(self, models: Sequence[ModelCapability]) -> None:
        self._models = list(models)

    def set_quota(self, quota: Sequence[QuotaRecord]) -> None:
        self._quota = list(quota)

    def set_health(self, health: ProviderHealth) -> None:
        self._health = health

    async def health_check(self) -> ProviderHealth:
        self.calls.append("health_check")
        return self._health

    async def get_quota(self) -> list[QuotaRecord]:
        self.calls.append("get_quota")
        return list(self._quota)

    async def list_models(self) -> list[ModelCapability]:
        self.calls.append("list_models")
        return list(self._models)

    async def classify_error(self, error: Exception) -> ErrorClass:
        self.calls.append("classify_error")
        if isinstance(error, FakeProviderError):
            return error.error_class
        return ErrorClass.UNKNOWN
