from __future__ import annotations

import httpx
import pytest

from app.opencode.config_check import allowed_small_models, check_small_model
from app.runtime.interfaces import SmallModelCheck

ALLOWED = frozenset({"groq/llama-x", "openrouter/openrouter/free"})


async def probe(handler):
    return await check_small_model(
        "http://127.0.0.1:4096/", ALLOWED, timeout_s=1, transport=httpx.MockTransport(handler)
    )


async def test_ok_and_url():
    seen = []

    def h(req):
        seen.append(str(req.url))
        return httpx.Response(200, json={"small_model": "groq/llama-x"})

    assert await probe(h) == SmallModelCheck.OK
    assert seen == ["http://127.0.0.1:4096/config"]


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"small_model": None},
        {"small_model": "anthropic/claude"},
        {"small_model": "groq/"},
        {"small_model": "groq/llama-x "},
        {"small_model": 5},
    ],
)
async def test_unsafe(body):
    assert await probe(lambda r: httpx.Response(200, json=body)) == SmallModelCheck.UNSAFE


async def test_unavailable():
    assert await probe(lambda r: httpx.Response(500)) == SmallModelCheck.UNAVAILABLE
    assert (
        await probe(lambda r: httpx.Response(200, content=b"nope")) == SmallModelCheck.UNAVAILABLE
    )
    assert await probe(lambda r: httpx.Response(200, json=[1])) == SmallModelCheck.UNAVAILABLE

    def boom(r):
        raise httpx.ConnectError("x")

    assert await probe(boom) == SmallModelCheck.UNAVAILABLE


def test_allowed_uses_provider_map_and_skips_disabled(make_config):
    cfg = make_config()
    allowed = allowed_small_models(cfg)
    assert "openrouter/openrouter/free" in allowed
    assert all("/" in a for a in allowed)
    disabled = make_config(
        routing={
            "policies": {
                pid: {**p.model_dump(), "enabled": False} for pid, p in cfg.routing.policies.items()
            }
        }
    )
    assert allowed_small_models(disabled) == frozenset()
