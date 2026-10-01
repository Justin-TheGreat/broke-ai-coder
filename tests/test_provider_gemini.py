from __future__ import annotations

import pytest
from adapter_helpers import (
    check_errors,
    check_health_matrix,
    check_missing_credential,
    check_secret_safety,
    make,
)
from conftest import SECRET, json_response

from app.providers.base import ErrorClass, ProviderAdapter
from app.providers.errors import MalformedResponseError, ProviderError
from app.providers.gemini import GEMINI_MAX_PAGES, GeminiAdapter
from app.router.policy import ModelRoutingStatus, model_listing


def _m(name, **kw):
    return {"name": f"models/{name}", "supportedGenerationMethods": ["generateContent"], **kw}


def test_protocol():
    adapter, _ = make(GeminiAdapter, lambda r: json_response(200, {}))
    assert isinstance(adapter, ProviderAdapter)
    assert adapter.provider_id == "gemini"


async def test_auth_header_not_in_url():
    adapter, rec = make(GeminiAdapter, lambda r: json_response(200, {"models": []}), key=SECRET)
    async with adapter:
        await adapter.list_models()
        await adapter.health_check()
    for req in rec.requests:
        assert req.headers["x-goog-api-key"] == SECRET
        assert "key=" not in str(req.url)
        assert "authorization" not in req.headers
        assert SECRET not in str(req.url)


async def test_list_models_parses_and_overlays():
    body = {
        "models": [
            _m("g-3.8", inputTokenLimit=1000000, outputTokenLimit=65536),
            {"name": "models/embed", "supportedGenerationMethods": ["embedContent"]},
            _m("g-3.7", inputTokenLimit="bad"),
            {"name": "models/no-methods"},
            "junk",
            {"nope": 1},
        ]
    }
    adapter, _ = make(
        GeminiAdapter,
        lambda r: json_response(200, body),
        key=SECRET,
        models={"g-3.7": {"supports_tool_calling": True, "context_window": 5}},
    )
    async with adapter:
        models = await adapter.list_models()
    by_id = {m.model: m for m in models}
    assert set(by_id) == {"g-3.8", "g-3.7", "no-methods"}
    assert by_id["g-3.8"].context_window == 1000000
    assert by_id["g-3.8"].max_output_tokens == 65536
    assert by_id["g-3.7"].supports_tool_calling and by_id["g-3.7"].context_window == 5


async def test_pagination_two_pages():
    def handler(request):
        if request.url.params.get("pageToken") == "t2":
            return json_response(200, {"models": [_m("b")]})
        return json_response(200, {"models": [_m("a")], "nextPageToken": "t2"})

    adapter, rec = make(GeminiAdapter, handler, key=SECRET)
    async with adapter:
        models = await adapter.list_models()
    assert [m.model for m in models] == ["a", "b"]
    assert len(rec.requests) == 2
    assert rec.requests[0].url.params["pageSize"] == "1000"


async def test_pagination_stops_at_max_pages(caplog):
    def handler(request):
        return json_response(200, {"models": [_m("a")], "nextPageToken": "again"})

    adapter, rec = make(GeminiAdapter, handler, key=SECRET)
    async with adapter:
        await adapter.list_models()
    assert len(rec.requests) == GEMINI_MAX_PAGES == 10
    assert "truncated" in caplog.text


async def test_unlisted_model_discovered_only(make_config):
    adapter, _ = make(
        GeminiAdapter,
        lambda r: json_response(200, {"models": [_m("g-3.8"), _m("g-pro")]}),
        key=SECRET,
    )
    async with adapter:
        models = await adapter.list_models()
    listing = {
        m.model: m.status for m in model_listing(make_config(), "gemini", [m.model for m in models])
    }
    assert listing["g-pro"] == ModelRoutingStatus.DISCOVERED_ONLY
    assert listing["g-3.8"] == ModelRoutingStatus.ALLOWED


async def test_list_models_malformed():
    adapter, _ = make(GeminiAdapter, lambda r: json_response(200, {"models": 3}), key=SECRET)
    async with adapter:
        with pytest.raises(MalformedResponseError):
            await adapter.list_models()


async def test_health():
    await check_health_matrix(GeminiAdapter, {"models": []})


async def test_get_quota_empty_no_request():
    adapter, rec = make(GeminiAdapter, lambda r: json_response(200, {}), key=SECRET)
    async with adapter:
        assert await adapter.get_quota() == []
    assert rec.requests == []


async def test_common_errors():
    await check_errors(GeminiAdapter)


async def test_gemini_specific_errors():
    def err(status, body, headers=None):
        adapter, _ = make(GeminiAdapter, lambda r: json_response(status, body, headers), key=SECRET)
        return adapter

    adapter = err(
        429,
        {
            "error": {
                "code": 429,
                "status": "RESOURCE_EXHAUSTED",
                "message": "quota",
                "details": [
                    {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "7s"}
                ],
            }
        },
    )
    async with adapter:
        with pytest.raises(ProviderError) as ei:
            await adapter.list_models()
    assert ei.value.error_class == ErrorClass.RATE_LIMITED
    assert ei.value.retry_after_s == 7
    assert ei.value.error_code == "RESOURCE_EXHAUSTED"

    # header wins over body RetryInfo
    adapter = err(
        429,
        {
            "error": {
                "status": "RESOURCE_EXHAUSTED",
                "message": "q",
                "details": [{"@type": "x.google.rpc.RetryInfo", "retryDelay": "7s"}],
            }
        },
        {"Retry-After": "3"},
    )
    async with adapter:
        with pytest.raises(ProviderError) as ei:
            await adapter.list_models()
    assert ei.value.retry_after_s == 3

    adapter = err(
        400,
        {
            "error": {
                "status": "INVALID_ARGUMENT",
                "message": "API key not valid",
                "details": [{"@type": "x", "reason": "API_KEY_INVALID"}],
            }
        },
    )
    async with adapter:
        with pytest.raises(ProviderError) as ei:
            await adapter.list_models()
    assert ei.value.error_class == ErrorClass.AUTH_FAILED

    adapter = err(404, {"error": {"status": "NOT_FOUND", "message": "nope"}})
    async with adapter:
        with pytest.raises(ProviderError) as ei:
            await adapter.list_models()
    assert ei.value.error_class == ErrorClass.MODEL_UNAVAILABLE


async def test_secret_safety(caplog):
    await check_secret_safety(GeminiAdapter, caplog)
    await check_missing_credential(GeminiAdapter)
