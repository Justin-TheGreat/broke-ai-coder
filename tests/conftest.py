from __future__ import annotations

from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest

from app.config.loader import parse_config
from app.config.models import AppConfig, ProviderConfig
from app.db.connection import open_database
from app.db.migrations import migrate
from app.providers.base import ModelCapability
from app.router.types import RouterState

NOW = datetime(2026, 9, 30, 21, 0, 0, tzinfo=UTC)
SECRET = "sk-test-SECRET-0123456789"


def secret_env() -> dict[str, str]:
    return {
        "OPENROUTER_API_KEY": SECRET + "-or",
        "GEMINI_API_KEY": SECRET + "-ge",
        "GROQ_API_KEY": SECRET + "-gq",
    }


def json_response(status: int, body: Any, headers: dict[str, str] | None = None) -> httpx.Response:
    return httpx.Response(status, json=body, headers=headers)


def provider_cfg(**kw: Any) -> ProviderConfig:
    data: dict[str, Any] = {"api_key_env": "TEST_API_KEY"}
    data.update(kw)
    return ProviderConfig(**data)


@pytest.fixture(autouse=True)
def _no_real_network(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _blocked(self: Any, request: Any) -> Any:
        raise RuntimeError("real network call attempted")

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", _blocked)


@pytest.fixture
def db(tmp_path) -> Iterator:
    conn = open_database(tmp_path / "t.db")
    migrate(conn)
    try:
        yield conn
    finally:
        conn.close()


def _base_config_dict() -> dict[str, Any]:
    return {
        "providers": {
            p: {"enabled": True, "api_key_env": f"{p.upper()}_API_KEY"}
            for p in ("openrouter", "gemini", "groq")
        },
        "routing": {
            "provider_order": [
                "openrouter-free",
                "gemini-free",
                "groq-free",
            ],
            "policies": {
                "openrouter-free": {
                    "provider": "openrouter",
                    "model_order": ["openrouter/free"],
                },
                "gemini-free": {
                    "provider": "gemini",
                    "model_order": ["g-3.8", "g-3.7"],
                },
                "groq-free": {
                    "provider": "groq",
                    "model_order": ["q-x", "q-y"],
                },
            },
        },
    }


@pytest.fixture
def make_config() -> Callable[..., AppConfig]:
    def _make(**overrides: Any) -> AppConfig:
        data = _base_config_dict()
        for key, value in overrides.items():
            if isinstance(value, dict) and isinstance(data.get(key), dict):
                data[key].update(value)
            else:
                data[key] = value
        return parse_config(data)

    return _make


def full_state(config: AppConfig, now: datetime = NOW, **kwargs: Any) -> RouterState:
    caps = {
        (pol.provider, m): ModelCapability(
            pol.provider,
            m,
            supports_tool_calling=True,
            context_window=200_000,
            max_output_tokens=32_000,
        )
        for pol in config.routing.policies.values()
        for m in pol.model_order
    }
    return RouterState(
        now=now,
        credentials_present=frozenset(config.providers),
        capabilities=caps,
        **kwargs,
    )
