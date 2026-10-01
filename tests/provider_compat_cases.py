from __future__ import annotations

import httpx
from adapter_helpers import make
from conftest import NOW, SECRET, json_response

from app.providers.base import ErrorClass, QuotaConfidence
from app.providers.errors import ProviderError


async def check_compat_errors(cls) -> None:
    async def run(handler):
        adapter, _ = make(cls, handler, key=SECRET)
        async with adapter:
            try:
                await adapter.list_models()
            except ProviderError as e:
                return adapter, e
        raise AssertionError("expected error")

    _, err = await run(
        lambda r: json_response(
            400, {"error": {"message": "too long", "code": "context_length_exceeded"}}
        )
    )
    assert err.error_class == ErrorClass.CONTEXT_TOO_LARGE
    _, err = await run(
        lambda r: json_response(404, {"error": {"message": "x", "code": "model_not_found"}})
    )
    assert err.error_class == ErrorClass.MODEL_UNAVAILABLE
    adapter, err = await run(lambda r: httpx.Response(429, content=b"not json"))
    assert err.error_class == ErrorClass.RATE_LIMITED
    assert await adapter.classify_error(err) == ErrorClass.RATE_LIMITED
    # type is used when code is not a string
    _, err = await run(
        lambda r: json_response(
            400, {"error": {"message": "x", "code": 7, "type": "insufficient_quota"}}
        )
    )
    assert err.error_class == ErrorClass.QUOTA_EXHAUSTED


async def check_quota_headers(cls, provider, headers, expect_windows) -> None:
    adapter, rec = make(cls, lambda r: json_response(200, {"data": []}, headers), key=SECRET)
    async with adapter:
        recs = await adapter.get_quota()
    assert rec.requests[0].url.path.endswith("/models")
    assert [r.window for r in recs] == expect_windows
    for r in recs:
        assert r.model is None
        assert r.source == f"{provider}:response-headers"
        assert r.confidence == QuotaConfidence.EXACT
        assert r.observed_at == NOW

    adapter, _ = make(cls, lambda r: json_response(200, {"data": []}), key=SECRET)
    async with adapter:
        assert await adapter.get_quota() == []
