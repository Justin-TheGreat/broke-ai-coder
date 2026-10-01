from __future__ import annotations

from conftest import NOW

from app.providers.base import (
    PAIR_FALLBACK_ERRORS,
    PROVIDER_FALLBACK_ERRORS,
    STOP_ERRORS,
    ErrorClass,
    HealthStatus,
    ModelCapability,
    ProviderAdapter,
    ProviderHealth,
    QuotaConfidence,
    QuotaRecord,
    QuotaUnit,
)
from app.providers.fake import FakeProviderAdapter, FakeProviderError


def test_protocol():
    assert isinstance(FakeProviderAdapter("x"), ProviderAdapter)


async def test_methods_return_configured_data():
    m = ModelCapability("x", "m1")
    q = QuotaRecord("x", None, "day", QuotaUnit.REQUESTS, QuotaConfidence.UNKNOWN, NOW, "t")
    h = ProviderHealth("x", HealthStatus.DEGRADED, NOW)
    a = FakeProviderAdapter("x", models=[m], quota=[q], health=h)
    assert await a.list_models() == [m]
    assert await a.get_quota() == [q]
    assert await a.health_check() == h
    assert a.calls == ["list_models", "get_quota", "health_check"]
    out = await a.list_models()
    out.clear()
    assert await a.list_models() == [m]
    a.set_models([])
    assert await a.list_models() == []


async def test_default_health_healthy():
    assert (await FakeProviderAdapter("x").health_check()).status == HealthStatus.HEALTHY


async def test_classify_error():
    a = FakeProviderAdapter("x")
    err = FakeProviderError(ErrorClass.RATE_LIMITED)
    assert await a.classify_error(err) == ErrorClass.RATE_LIMITED
    assert await a.classify_error(ValueError("x")) == ErrorClass.UNKNOWN


def test_error_partition_complete_and_disjoint():
    sets = [PAIR_FALLBACK_ERRORS, PROVIDER_FALLBACK_ERRORS, STOP_ERRORS]
    assert sum(len(s) for s in sets) == len(ErrorClass)
    assert set().union(*sets) == set(ErrorClass)
