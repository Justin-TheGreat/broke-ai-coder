from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime
from typing import ClassVar

import httpx

from app.config.models import ProviderConfig
from app.providers.base import ErrorClass, ModelCapability, QuotaDimension, QuotaRecord
from app.providers.errors import MalformedResponseError
from app.providers.http import (
    OPENAI_COMPAT_CODE_MAP,
    HttpProviderAdapter,
    apply_metadata,
    positive_int,
)
from app.providers.ratelimit import parse_rate_limit_headers
from app.redaction import SecretRedactor
from app.timeutil import utcnow

PROVIDER_ID = "groq"

GROQ_HEADER_DIMENSIONS: Mapping[str, QuotaDimension] = {
    "requests": QuotaDimension.REQUESTS_PER_DAY,
    "tokens": QuotaDimension.TOKENS_PER_MINUTE,
}


class GroqAdapter(HttpProviderAdapter):
    DEFAULT_BASE_URL: ClassVar[str] = "https://api.groq.com/openai/v1"
    HEALTH_PATH: ClassVar[str] = "models"
    ERROR_CODE_MAP: ClassVar[Mapping[str, ErrorClass]] = OPENAI_COMPAT_CODE_MAP

    def __init__(
        self,
        config: ProviderConfig,
        *,
        provider_id: str = PROVIDER_ID,
        transport: httpx.AsyncBaseTransport | None = None,
        environ: Mapping[str, str] | None = None,
        clock: Callable[[], datetime] = utcnow,
        redactor: SecretRedactor | None = None,
    ) -> None:
        super().__init__(
            provider_id,
            config,
            transport=transport,
            environ=environ,
            clock=clock,
            redactor=redactor,
        )

    async def list_models(self) -> list[ModelCapability]:
        data, _ = await self._get_json("models")
        items = data.get("data") if isinstance(data, dict) else None
        if not isinstance(items, list):
            raise MalformedResponseError(self.provider_id, "unexpected models response shape")
        out: list[ModelCapability] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            model_id = item.get("id")
            if not isinstance(model_id, str) or not model_id:
                continue
            if item.get("active") is False:
                continue
            cap = ModelCapability(
                provider=self.provider_id,
                model=model_id,
                context_window=positive_int(item.get("context_window")),
                max_output_tokens=positive_int(item.get("max_completion_tokens")),
            )
            out.append(apply_metadata(cap, self._config.models.get(model_id)))
        return out

    async def get_quota(self) -> list[QuotaRecord]:
        _, resp = await self._get_json("models")
        return parse_rate_limit_headers(
            resp.headers,
            GROQ_HEADER_DIMENSIONS,
            provider=self.provider_id,
            model=None,
            now=self._clock(),
        )
