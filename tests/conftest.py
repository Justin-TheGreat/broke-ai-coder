from __future__ import annotations

from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from typing import Any

import pytest

from app.config.loader import parse_config
from app.config.models import AppConfig
from app.db.connection import open_database
from app.db.migrations import migrate
from app.providers.base import ModelCapability
from app.router.types import RouterState

NOW = datetime(2026, 9, 30, 21, 0, 0, tzinfo=UTC)


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
            for p in ("openrouter", "gemini", "cerebras", "groq")
        },
        "routing": {
            "provider_order": [
                "openrouter-free",
                "gemini-free",
                "cerebras-free",
                "groq-free",
                "openrouter-paid",
            ],
            "policies": {
                "openrouter-free": {
                    "provider": "openrouter",
                    "cost_class": "FREE",
                    "model_order": ["openrouter/free"],
                },
                "gemini-free": {
                    "provider": "gemini",
                    "cost_class": "FREE",
                    "model_order": ["g-3.8", "g-3.7"],
                },
                "cerebras-free": {
                    "provider": "cerebras",
                    "cost_class": "FREE",
                    "model_order": ["c-a", "c-b"],
                },
                "groq-free": {
                    "provider": "groq",
                    "cost_class": "FREE",
                    "model_order": ["q-x", "q-y"],
                },
                "openrouter-paid": {
                    "provider": "openrouter",
                    "cost_class": "PAID",
                    "model_order": ["or-paid-1"],
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
