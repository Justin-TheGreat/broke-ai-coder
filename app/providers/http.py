from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from dataclasses import replace
from datetime import datetime
from types import TracebackType
from typing import ClassVar

import httpx

from app.config.models import ModelMetadataConfig, ProviderConfig
from app.config.secrets import resolve_secret
from app.providers.base import ErrorClass, HealthStatus, ModelCapability, ProviderHealth
from app.providers.errors import (
    MAX_MESSAGE_CHARS,
    MalformedResponseError,
    MissingCredentialError,
    ProviderError,
)
from app.providers.ratelimit import parse_retry_after
from app.redaction import REDACTED, SecretRedactor
from app.timeutil import utcnow

logger = logging.getLogger(__name__)

COMMON_STATUS_MAP: Mapping[int, ErrorClass] = {
    400: ErrorClass.INVALID_REQUEST,
    401: ErrorClass.AUTH_FAILED,
    403: ErrorClass.AUTH_FAILED,
    404: ErrorClass.MODEL_UNAVAILABLE,
    408: ErrorClass.TRANSIENT_NETWORK,
    413: ErrorClass.CONTEXT_TOO_LARGE,
    422: ErrorClass.INVALID_REQUEST,
    429: ErrorClass.RATE_LIMITED,
    500: ErrorClass.PROVIDER_UNAVAILABLE,
    502: ErrorClass.PROVIDER_UNAVAILABLE,
    503: ErrorClass.PROVIDER_UNAVAILABLE,
    504: ErrorClass.TIMEOUT_UNKNOWN_OUTCOME,
}
OPENAI_COMPAT_CODE_MAP: Mapping[str, ErrorClass] = {
    "context_length_exceeded": ErrorClass.CONTEXT_TOO_LARGE,
    "model_not_found": ErrorClass.MODEL_UNAVAILABLE,
    "rate_limit_exceeded": ErrorClass.RATE_LIMITED,
    "insufficient_quota": ErrorClass.QUOTA_EXHAUSTED,
    "invalid_api_key": ErrorClass.AUTH_FAILED,
}
REQUEST_ID_HEADERS = ("x-request-id", "request-id")


def classify_status(status: int) -> ErrorClass:
    mapped = COMMON_STATUS_MAP.get(status)
    if mapped is not None:
        return mapped
    if 400 <= status < 500:
        return ErrorClass.INVALID_REQUEST
    if 500 <= status < 600:
        return ErrorClass.PROVIDER_UNAVAILABLE
    return ErrorClass.UNKNOWN


def classify_transport_error(e: BaseException) -> ErrorClass:
    if isinstance(
        e, httpx.ConnectTimeout | httpx.ConnectError | httpx.PoolTimeout | httpx.ProxyError
    ):
        return ErrorClass.TRANSIENT_NETWORK
    if isinstance(
        e,
        httpx.ReadTimeout
        | httpx.WriteTimeout
        | httpx.ReadError
        | httpx.WriteError
        | httpx.RemoteProtocolError
        | TimeoutError,
    ):
        return ErrorClass.TIMEOUT_UNKNOWN_OUTCOME
    return ErrorClass.UNKNOWN


def apply_metadata(cap: ModelCapability, meta: ModelMetadataConfig | None) -> ModelCapability:
    if meta is None:
        return cap
    overrides = {k: v for k, v in meta.model_dump().items() if v is not None}
    return replace(cap, **overrides) if overrides else cap


def positive_int(value: object) -> int | None:
    """Accept only a real int > 0 (not a bool); everything else is unknown."""
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return value


class HttpProviderAdapter:
    DEFAULT_BASE_URL: ClassVar[str]
    HEALTH_PATH: ClassVar[str]
    HEALTH_PARAMS: ClassVar[Mapping[str, str]] = {}
    ERROR_CODE_MAP: ClassVar[Mapping[str, ErrorClass]] = {}
    STATUS_OVERRIDES: ClassVar[Mapping[int, ErrorClass]] = {}

    def __init__(
        self,
        provider_id: str,
        config: ProviderConfig,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        environ: Mapping[str, str] | None = None,
        clock: Callable[[], datetime] = utcnow,
        redactor: SecretRedactor | None = None,
    ) -> None:
        self.provider_id = provider_id
        self._config = config
        self._transport = transport
        self._environ = environ
        self._clock = clock
        self._redactor = redactor
        self._client: httpx.AsyncClient | None = None

    @property
    def base_url(self) -> str:
        return self._config.base_url or self.DEFAULT_BASE_URL

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(provider_id={self.provider_id!r}, base_url={self.base_url!r})"
        )

    async def aclose(self) -> None:
        client, self._client = self._client, None
        if client is not None:
            await client.aclose()

    async def __aenter__(self) -> HttpProviderAdapter:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()

    # ---- hooks -----------------------------------------------------

    def _auth_headers(self, api_key: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {api_key}"}

    def _parse_error_body(self, body: object) -> tuple[str | None, str, float | None]:
        if not isinstance(body, dict):
            return None, "", None
        err = body.get("error")
        if isinstance(err, str):
            return None, err, None
        if not isinstance(err, dict):
            return None, "", None
        code = err.get("code")
        if not isinstance(code, str):
            code = err.get("type")
        if not isinstance(code, str):
            code = None
        message = err.get("message")
        return code, message if isinstance(message, str) else "", None

    # ---- plumbing --------------------------------------------------

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self.base_url,
                timeout=httpx.Timeout(self._config.timeout_s),
                transport=self._transport,
                follow_redirects=False,
            )
        return self._client

    def _api_key(self) -> str | None:
        return resolve_secret(self._config.api_key_env, self._environ)

    async def _get_json(
        self, path: str, params: Mapping[str, str | int] | None = None
    ) -> tuple[object, httpx.Response]:
        api_key = self._api_key()
        if api_key is None:
            raise MissingCredentialError(self.provider_id, self._config.api_key_env)
        transport_class: ErrorClass | None = None
        transport_name = ""
        response: httpx.Response | None = None
        try:
            response = await self._get_client().get(
                path, params=dict(params) if params else None, headers=self._auth_headers(api_key)
            )
        except httpx.HTTPError as e:
            transport_class = classify_transport_error(e)
            transport_name = type(e).__name__
        # Raised outside the except block so no request object (with headers)
        # is reachable via __context__ either.
        if response is None:
            raise ProviderError(
                self.provider_id,
                transport_class or ErrorClass.UNKNOWN,
                f"network: {transport_name}",
            )
        logger.debug("provider=%s GET %s status=%d", self.provider_id, path, response.status_code)
        if not 200 <= response.status_code < 300:
            raise self.error_from_response(response)
        try:
            data = response.json()
        except ValueError:
            data = None
            malformed = True
        else:
            malformed = False
        if malformed:
            raise MalformedResponseError(
                self.provider_id, "response is not valid JSON", status_code=response.status_code
            )
        return data, response

    def error_from_response(self, response: httpx.Response) -> ProviderError:
        status = response.status_code
        try:
            body: object = response.json()
        except ValueError:
            body = None
        if isinstance(body, dict):
            code, message, body_retry = self._parse_error_body(body)
            message = message or f"HTTP {status}"
        else:
            code, message, body_retry = None, f"HTTP {status}", None
        retry_after = parse_retry_after(response.headers.get("retry-after"), self._clock())
        if retry_after is None:
            retry_after = body_retry
        request_id = next(
            (response.headers[h] for h in REQUEST_ID_HEADERS if h in response.headers), None
        )
        error_class: ErrorClass | None = None
        if code:
            error_class = self.ERROR_CODE_MAP.get(code.lower())
        if error_class is None:
            error_class = self.STATUS_OVERRIDES.get(status)
        if error_class is None:
            error_class = classify_status(status)
        if self._redactor is not None:
            message = self._redactor.redact(message)
        api_key = self._api_key()
        if api_key:
            message = message.replace(api_key, REDACTED)
        if code and api_key:
            code = code.replace(api_key, REDACTED)
        return ProviderError(
            self.provider_id,
            error_class,
            message[:MAX_MESSAGE_CHARS],
            status_code=status,
            error_code=code[:100] if code else None,
            retry_after_s=retry_after,
            request_id=request_id,
        )

    async def classify_error(self, error: Exception) -> ErrorClass:
        try:
            if isinstance(error, ProviderError):
                return error.error_class
            if isinstance(error, httpx.HTTPStatusError):
                return self.error_from_response(error.response).error_class
            if isinstance(error, httpx.HTTPError | TimeoutError):
                return classify_transport_error(error)
        except Exception:
            return ErrorClass.UNKNOWN
        return ErrorClass.UNKNOWN

    async def health_check(self) -> ProviderHealth:
        def result(status: HealthStatus, detail: str) -> ProviderHealth:
            return ProviderHealth(self.provider_id, status, self._clock(), detail)

        try:
            data, _ = await self._get_json(self.HEALTH_PATH, self.HEALTH_PARAMS)
            if isinstance(data, dict):
                return result(HealthStatus.HEALTHY, "ok")
            return result(HealthStatus.DEGRADED, "unexpected response shape")
        except MissingCredentialError as e:
            return result(HealthStatus.DOWN, e.message)
        except MalformedResponseError:
            return result(HealthStatus.DEGRADED, "malformed response")
        except ProviderError as e:
            detail = f"HTTP {e.status_code}" if e.status_code is not None else str(e.error_class)
            if e.error_class in (
                ErrorClass.AUTH_FAILED,
                ErrorClass.PROVIDER_UNAVAILABLE,
                ErrorClass.TRANSIENT_NETWORK,
                ErrorClass.TIMEOUT_UNKNOWN_OUTCOME,
            ):
                return result(HealthStatus.DOWN, detail)
            return result(HealthStatus.DEGRADED, detail)
        except Exception as e:
            return result(HealthStatus.DOWN, f"error: {type(e).__name__}")
