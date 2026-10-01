from __future__ import annotations

import math
from collections.abc import Callable, Mapping
from datetime import datetime
from typing import ClassVar

import httpx

from app.config.models import ProviderConfig
from app.providers.base import (
    ErrorClass,
    ModelCapability,
    QuotaConfidence,
    QuotaRecord,
    QuotaUnit,
)
from app.providers.errors import MalformedResponseError
from app.providers.http import HttpProviderAdapter, apply_metadata, positive_int
from app.redaction import SecretRedactor
from app.timeutil import utcnow

PROVIDER_ID = "openrouter"


def _usd(value: object) -> int | float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    if not math.isfinite(value) or value < 0:
        return None
    return value


class OpenRouterAdapter(HttpProviderAdapter):
    DEFAULT_BASE_URL: ClassVar[str] = "https://openrouter.ai/api/v1"
    HEALTH_PATH: ClassVar[str] = "key"
    STATUS_OVERRIDES: ClassVar[Mapping[int, ErrorClass]] = {
        402: ErrorClass.QUOTA_EXHAUSTED,
        403: ErrorClass.POLICY_REJECTED,
    }

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
            params = item.get("supported_parameters")
            params = params if isinstance(params, list) else []
            arch = item.get("architecture")
            modalities = arch.get("input_modalities") if isinstance(arch, dict) else None
            modalities = modalities if isinstance(modalities, list) else []
            top = item.get("top_provider")
            max_out = (
                positive_int(top.get("max_completion_tokens")) if isinstance(top, dict) else None
            )
            cap = ModelCapability(
                provider=self.provider_id,
                model=model_id,
                supports_tool_calling="tools" in params,
                supports_structured_output="structured_outputs" in params
                or "response_format" in params,
                supports_vision="image" in modalities,
                context_window=positive_int(item.get("context_length")),
                max_output_tokens=max_out,
            )
            out.append(apply_metadata(cap, self._config.models.get(model_id)))
        return out

    async def get_quota(self) -> list[QuotaRecord]:
        data, _ = await self._get_json("key")
        info = data.get("data") if isinstance(data, dict) else None
        if not isinstance(info, dict):
            raise MalformedResponseError(self.provider_id, "unexpected key response shape")
        now = self._clock()
        limit = _usd(info.get("limit"))
        used = _usd(info.get("usage"))
        source = f"{self.provider_id}:key"
        if limit is None:
            return [
                QuotaRecord(
                    self.provider_id,
                    None,
                    "total",
                    QuotaUnit.USD,
                    QuotaConfidence.UNKNOWN,
                    now,
                    source,
                    limit=None,
                    used=used,
                    remaining=None,
                )
            ]
        remaining = _usd(info.get("limit_remaining"))
        if remaining is None:
            return [
                QuotaRecord(
                    self.provider_id,
                    None,
                    "total",
                    QuotaUnit.USD,
                    QuotaConfidence.UNKNOWN,
                    now,
                    source,
                    limit=limit,
                    used=used,
                    remaining=None,
                )
            ]
        return [
            QuotaRecord(
                self.provider_id,
                None,
                "total",
                QuotaUnit.USD,
                QuotaConfidence.EXACT,
                now,
                source,
                limit=limit,
                used=used,
                remaining=remaining,
            )
        ]
