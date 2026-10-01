from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from datetime import datetime
from typing import ClassVar

import httpx

from app.config.models import ProviderConfig
from app.providers.base import ErrorClass, ModelCapability, QuotaRecord
from app.providers.errors import MalformedResponseError
from app.providers.http import HttpProviderAdapter, apply_metadata, positive_int
from app.providers.ratelimit import parse_duration
from app.redaction import SecretRedactor
from app.timeutil import utcnow

logger = logging.getLogger(__name__)

PROVIDER_ID = "gemini"
GEMINI_MAX_PAGES = 10
GEMINI_PAGE_SIZE = 1000


class GeminiAdapter(HttpProviderAdapter):
    DEFAULT_BASE_URL: ClassVar[str] = "https://generativelanguage.googleapis.com/v1beta"
    HEALTH_PATH: ClassVar[str] = "models"
    HEALTH_PARAMS: ClassVar[Mapping[str, str]] = {"pageSize": "1"}
    ERROR_CODE_MAP: ClassVar[Mapping[str, ErrorClass]] = {
        "api_key_invalid": ErrorClass.AUTH_FAILED,
        "resource_exhausted": ErrorClass.RATE_LIMITED,
        "unauthenticated": ErrorClass.AUTH_FAILED,
        "permission_denied": ErrorClass.AUTH_FAILED,
        "not_found": ErrorClass.MODEL_UNAVAILABLE,
        "invalid_argument": ErrorClass.INVALID_REQUEST,
        "unavailable": ErrorClass.PROVIDER_UNAVAILABLE,
        "internal": ErrorClass.PROVIDER_UNAVAILABLE,
        "deadline_exceeded": ErrorClass.TIMEOUT_UNKNOWN_OUTCOME,
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

    def _auth_headers(self, api_key: str) -> dict[str, str]:
        return {"x-goog-api-key": api_key}

    def _parse_error_body(self, body: object) -> tuple[str | None, str, float | None]:
        err = body.get("error") if isinstance(body, dict) else None
        if not isinstance(err, dict):
            return None, "", None
        status = err.get("status")
        code = status if isinstance(status, str) else None
        message = err.get("message")
        message = message if isinstance(message, str) else ""
        retry: float | None = None
        details = err.get("details")
        if isinstance(details, list):
            for item in details:
                if not isinstance(item, dict):
                    continue
                if item.get("reason") == "API_KEY_INVALID":
                    code = "API_KEY_INVALID"
                type_url = item.get("@type")
                if (
                    retry is None
                    and isinstance(type_url, str)
                    and type_url.endswith("google.rpc.RetryInfo")
                    and isinstance(item.get("retryDelay"), str)
                ):
                    retry = parse_duration(item["retryDelay"])
        return code, message, retry

    async def list_models(self) -> list[ModelCapability]:
        out: list[ModelCapability] = []
        page_token: str | None = None
        for _ in range(GEMINI_MAX_PAGES):
            params: dict[str, str | int] = {"pageSize": GEMINI_PAGE_SIZE}
            if page_token:
                params["pageToken"] = page_token
            data, _ = await self._get_json("models", params)
            items = data.get("models") if isinstance(data, dict) else None
            if not isinstance(items, list):
                raise MalformedResponseError(self.provider_id, "unexpected models response shape")
            for item in items:
                cap = self._parse_model(item)
                if cap is not None:
                    out.append(cap)
            token = data.get("nextPageToken") if isinstance(data, dict) else None
            page_token = token if isinstance(token, str) and token else None
            if page_token is None:
                break
        else:
            logger.warning(
                "provider=%s model listing truncated at %d pages",
                self.provider_id,
                GEMINI_MAX_PAGES,
            )
        return out

    def _parse_model(self, item: object) -> ModelCapability | None:
        if not isinstance(item, dict):
            return None
        name = item.get("name")
        if not isinstance(name, str) or not name:
            return None
        model_id = name.removeprefix("models/")
        if not model_id:
            return None
        methods = item.get("supportedGenerationMethods")
        if isinstance(methods, list) and "generateContent" not in methods:
            return None
        cap = ModelCapability(
            provider=self.provider_id,
            model=model_id,
            context_window=positive_int(item.get("inputTokenLimit")),
            max_output_tokens=positive_int(item.get("outputTokenLimit")),
        )
        return apply_metadata(cap, self._config.models.get(model_id))

    async def get_quota(self) -> list[QuotaRecord]:
        return []
