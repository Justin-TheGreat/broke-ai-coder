from __future__ import annotations

import pytest
from adapter_helpers import (
    check_errors,
    check_health_matrix,
    check_missing_credential,
    check_secret_safety,
    make,
)
from conftest import NOW, SECRET, json_response

from app.providers.base import (
    ErrorClass,
    ProviderAdapter,
    QuotaConfidence,
    QuotaUnit,
)
from app.providers.errors import MalformedResponseError, ProviderError
from app.providers.openrouter import OpenRouterAdapter
from app.router.policy import ModelRoutingStatus, model_listing

MODELS = {
    "data": [
        {
            "id": "openrouter/free",
            "context_length": 200000,
            "top_provider": {"max_completion_tokens": 8000},
            "supported_parameters": ["tools", "structured_outputs"],
            "architecture": {"input_modalities": ["text", "image"]},
        },
        {"id": "other/model", "context_length": True, "supported_parameters": ["response_format"]},
        "junk",
        {"no_id": 1},
        {"id": 5},
    ]
}


def test_protocol():
    adapter, _ = make(OpenRouterAdapter, lambda r: json_response(200, {}))
    assert isinstance(adapter, ProviderAdapter)
    assert adapter.provider_id == "openrouter"


async def test_auth_header():
    adapter, rec = make(OpenRouterAdapter, lambda r: json_response(200, MODELS), key=SECRET)
    async with adapter:
        await adapter.list_models()
    assert rec.requests[0].headers["Authorization"] == f"Bearer {SECRET}"
    assert str(rec.requests[0].url) == "https://openrouter.ai/api/v1/models"


async def test_list_models():
    adapter, _ = make(
        OpenRouterAdapter,
        lambda r: json_response(200, MODELS),
        key=SECRET,
        models={"other/model": {"supports_vision": True, "context_window": 99}},
    )
    async with adapter:
        models = await adapter.list_models()
    by_id = {m.model: m for m in models}
    assert set(by_id) == {"openrouter/free", "other/model"}
    free = by_id["openrouter/free"]
    assert free.supports_tool_calling and free.supports_structured_output and free.supports_vision
    assert free.context_window == 200000 and free.max_output_tokens == 8000
    other = by_id["other/model"]
    assert other.supports_structured_output and not other.supports_tool_calling
    assert other.supports_vision and other.context_window == 99  # config overlay wins


async def test_unlisted_model_discovered_only(make_config):
    adapter, _ = make(OpenRouterAdapter, lambda r: json_response(200, MODELS), key=SECRET)
    async with adapter:
        models = await adapter.list_models()
    listing = {
        m.model: m.status
        for m in model_listing(make_config(), "openrouter", [m.model for m in models])
    }
    assert listing["other/model"] == ModelRoutingStatus.DISCOVERED_ONLY
    assert listing["openrouter/free"] == ModelRoutingStatus.ALLOWED


async def test_list_models_malformed():
    adapter, _ = make(OpenRouterAdapter, lambda r: json_response(200, {"data": "x"}), key=SECRET)
    async with adapter:
        with pytest.raises(MalformedResponseError) as ei:
            await adapter.list_models()
    assert ei.value.error_class == ErrorClass.UNKNOWN


async def test_health():
    await check_health_matrix(OpenRouterAdapter, {"data": {}})


async def test_get_quota_numeric():
    body = {"data": {"limit": 10.0, "usage": 2.5, "limit_remaining": 7.5}}
    adapter, rec = make(OpenRouterAdapter, lambda r: json_response(200, body), key=SECRET)
    async with adapter:
        (rec_,) = await adapter.get_quota()
    assert rec.requests[0].url.path.endswith("/key")
    assert rec_.confidence == QuotaConfidence.EXACT
    assert (rec_.limit, rec_.used, rec_.remaining) == (10.0, 2.5, 7.5)
    assert rec_.unit == QuotaUnit.USD and rec_.window == "total" and rec_.model is None
    assert rec_.source == "openrouter:key" and rec_.observed_at == NOW


async def test_get_quota_null_limit():
    body = {"data": {"limit": None, "usage": 1.0, "limit_remaining": None}}
    adapter, _ = make(OpenRouterAdapter, lambda r: json_response(200, body), key=SECRET)
    async with adapter:
        (rec_,) = await adapter.get_quota()
    assert rec_.confidence == QuotaConfidence.UNKNOWN
    assert rec_.remaining is None and rec_.limit is None and rec_.used == 1.0


async def test_get_quota_malformed():
    adapter, _ = make(OpenRouterAdapter, lambda r: json_response(200, {"data": []}), key=SECRET)
    async with adapter:
        with pytest.raises(MalformedResponseError):
            await adapter.get_quota()


async def test_errors():
    await check_errors(OpenRouterAdapter)


@pytest.mark.parametrize(
    ("status", "expected"),
    [(402, ErrorClass.QUOTA_EXHAUSTED), (403, ErrorClass.POLICY_REJECTED)],
)
async def test_openrouter_status_overrides(status, expected):
    adapter, _ = make(
        OpenRouterAdapter,
        lambda r: json_response(status, {"error": {"code": status, "message": "m"}}),
        key=SECRET,
    )
    async with adapter:
        with pytest.raises(ProviderError) as ei:
            await adapter.list_models()
        assert ei.value.error_class == expected
        assert await adapter.classify_error(ei.value) == expected


async def test_secret_safety(caplog):
    await check_secret_safety(OpenRouterAdapter, caplog)
    await check_missing_credential(OpenRouterAdapter)
