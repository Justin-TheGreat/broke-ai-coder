"""Shared helpers for tests of the OpenCode server-config preflight."""

from __future__ import annotations

from typing import Any

from app.config.models import AppConfig
from app.opencode.config_check import opencode_model_ids
from app.opencode.server import build_opencode_config

GATEWAY_URL = "http://127.0.0.1:50999/v1"
GATEWAY_TOKEN = "gw-test-token-0123456789"


def good_config(config: AppConfig) -> dict[str, Any]:
    """What GET /config returns for a correctly pinned managed server."""
    return build_opencode_config(config, gateway_base_url=GATEWAY_URL, gateway_token=GATEWAY_TOKEN)


def safe_model(config: AppConfig) -> str:
    return sorted(opencode_model_ids(config))[0]
