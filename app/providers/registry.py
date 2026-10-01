from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from datetime import datetime

import httpx

from app.config.models import AppConfig
from app.providers.gemini import GeminiAdapter
from app.providers.groq import GroqAdapter
from app.providers.http import HttpProviderAdapter
from app.providers.openrouter import OpenRouterAdapter
from app.redaction import SecretRedactor
from app.timeutil import utcnow

logger = logging.getLogger(__name__)

ADAPTER_TYPES: Mapping[str, type[HttpProviderAdapter]] = {
    "openrouter": OpenRouterAdapter,
    "gemini": GeminiAdapter,
    "groq": GroqAdapter,
}


def build_adapters(
    config: AppConfig,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    environ: Mapping[str, str] | None = None,
    clock: Callable[[], datetime] = utcnow,
    redactor: SecretRedactor | None = None,
) -> dict[str, HttpProviderAdapter]:
    out: dict[str, HttpProviderAdapter] = {}
    for name, prov in config.providers.items():
        if not prov.enabled:
            continue
        adapter_type = ADAPTER_TYPES.get(name)
        if adapter_type is None:
            logger.warning("no adapter for provider %s", name)
            continue
        out[name] = adapter_type(  # type: ignore[call-arg]
            prov,
            provider_id=name,
            transport=transport,
            environ=environ,
            clock=clock,
            redactor=redactor,
        )
    return out
