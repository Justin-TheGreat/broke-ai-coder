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
from app.providers.groq import GroqAdapter
from app.router.policy import ModelRoutingStatus, model_listing

BODY = {
    "data": [
        {"id": "q-x", "context_window": 131072, "max_completion_tokens": 8192, "active": True},
        {"id": "q-old", "active": False},
        {"id": "q-extra", "context_window": "bad"},
        "junk",
        {"nope": 1},
    ]
}


def test_protocol():
    adapter, _ = make(GroqAdapter, lambda r: json_response(200, {}))
    assert isinstance(adapter, ProviderAdapter)
    assert adapter.provider_id == "groq"


async def test_auth_header():
    adapter, rec = make(GroqAdapter, lambda r: json_response(200, BODY), key=SECRET)
    async with adapter:
        await adapter.list_models()
    assert rec.requests[0].headers["Authorization"] == f"Bearer {SECRET}"
    assert str(rec.requests[0].url) == "https://api.groq.com/openai/v1/models"


async def test_list_models():
    adapter, _ = make(
        GroqAdapter,
        lambda r: json_response(200, BODY),
        key=SECRET,
        models={"q-extra": {"supports_vision": True}},
    )
    async with adapter:
        models = await adapter.list_models()
    by_id = {m.model: m for m in models}
    assert set(by_id) == {"q-x", "q-extra"}  # inactive and junk skipped
    assert by_id["q-x"].context_window == 131072
    assert by_id["q-x"].max_output_tokens == 8192
    assert by_id["q-extra"].context_window is None
    assert by_id["q-extra"].supports_vision is True


async def test_unlisted_model_discovered_only(make_config):
    adapter, _ = make(GroqAdapter, lambda r: json_response(200, BODY), key=SECRET)
    async with adapter:
        models = await adapter.list_models()
    listing = {
        m.model: m.status for m in model_listing(make_config(), "groq", [m.model for m in models])
    }
    assert listing["q-extra"] == ModelRoutingStatus.DISCOVERED_ONLY
    assert listing["q-x"] == ModelRoutingStatus.ALLOWED


async def test_health():
    await check_health_matrix(GroqAdapter, {"data": []})


async def test_quota_headers():
    headers = {
        "x-ratelimit-limit-requests": "14400",
        "x-ratelimit-remaining-requests": "14000",
        "x-ratelimit-reset-requests": "2m59.56s",
        "x-ratelimit-limit-tokens": "6000",
        "x-ratelimit-remaining-tokens": "5000",
        "x-ratelimit-reset-tokens": "7.66s",
    }
    await check_quota_headers(GroqAdapter, "groq", headers, ["day", "minute"])


async def test_errors():
    await check_errors(GroqAdapter)
    await check_compat_errors(GroqAdapter)


async def test_secret_safety(caplog):
    await check_secret_safety(GroqAdapter, caplog)
    await check_missing_credential(GroqAdapter)
