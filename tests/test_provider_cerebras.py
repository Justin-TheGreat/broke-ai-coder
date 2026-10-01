from __future__ import annotations

from adapter_helpers import (
    check_errors,
    check_health_matrix,
    check_missing_credential,
    check_secret_safety,
    make,
)
from conftest import SECRET, json_response
from provider_compat_cases import check_compat_errors, check_quota_headers

from app.providers.base import ProviderAdapter
from app.providers.cerebras import CerebrasAdapter
from app.router.policy import ModelRoutingStatus, model_listing

BODY = {"data": [{"id": "c-a"}, {"id": "c-extra"}, "junk", {"nope": 1}]}


def test_protocol():
    adapter, _ = make(CerebrasAdapter, lambda r: json_response(200, {}))
    assert isinstance(adapter, ProviderAdapter)
    assert adapter.provider_id == "cerebras"


async def test_auth_header():
    adapter, rec = make(CerebrasAdapter, lambda r: json_response(200, BODY), key=SECRET)
    async with adapter:
        await adapter.list_models()
    assert rec.requests[0].headers["Authorization"] == f"Bearer {SECRET}"
    assert str(rec.requests[0].url) == "https://api.cerebras.ai/v1/models"


async def test_list_models_defaults_and_overlay():
    adapter, _ = make(
        CerebrasAdapter,
        lambda r: json_response(200, BODY),
        key=SECRET,
        models={"c-a": {"supports_tool_calling": True, "context_window": 8192}},
    )
    async with adapter:
        models = await adapter.list_models()
    by_id = {m.model: m for m in models}
    assert set(by_id) == {"c-a", "c-extra"}
    assert by_id["c-extra"].supports_tool_calling is False
    assert by_id["c-extra"].context_window is None
    assert by_id["c-a"].supports_tool_calling is True
    assert by_id["c-a"].context_window == 8192


async def test_unlisted_model_discovered_only(make_config):
    adapter, _ = make(CerebrasAdapter, lambda r: json_response(200, BODY), key=SECRET)
    async with adapter:
        models = await adapter.list_models()
    listing = {
        m.model: m.status
        for m in model_listing(make_config(), "cerebras", [m.model for m in models])
    }
    assert listing["c-extra"] == ModelRoutingStatus.DISCOVERED_ONLY


async def test_health():
    await check_health_matrix(CerebrasAdapter, {"data": []})


async def test_quota_headers():
    headers = {
        "x-ratelimit-limit-requests-day": "1000",
        "x-ratelimit-remaining-requests-day": "990",
        "x-ratelimit-reset-requests-day": "3600.5",
        "x-ratelimit-limit-tokens-minute": "60000",
        "x-ratelimit-remaining-tokens-minute": "59000",
        "x-ratelimit-reset-tokens-minute": "30",
    }
    await check_quota_headers(CerebrasAdapter, "cerebras", headers, ["day", "minute"])


async def test_errors():
    await check_errors(CerebrasAdapter)
    await check_compat_errors(CerebrasAdapter)


async def test_secret_safety(caplog):
    await check_secret_safety(CerebrasAdapter, caplog)
    await check_missing_credential(CerebrasAdapter)
