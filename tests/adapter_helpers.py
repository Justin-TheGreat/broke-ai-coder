from __future__ import annotations

import logging
import traceback
from collections.abc import Callable
from typing import Any

import httpx
from conftest import NOW, SECRET, json_response, provider_cfg

from app.providers.base import HealthStatus
from app.providers.errors import MissingCredentialError, ProviderError
from app.providers.http import HttpProviderAdapter

ENV_NAME = "TEST_API_KEY"


class Recorder:
    """Wraps a handler, records every request."""

    def __init__(self, handler: Callable[[httpx.Request], httpx.Response]) -> None:
        self._handler = handler
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self._handler(request)

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self)


def make(cls: type[HttpProviderAdapter], handler, key: str | None = None, **cfg: Any):
    rec = Recorder(handler)
    environ = {} if key is None else {ENV_NAME: key}
    adapter = cls(provider_cfg(**cfg), transport=rec.transport, environ=environ, clock=lambda: NOW)
    return adapter, rec


def raiser(exc: Exception) -> Callable[[httpx.Request], httpx.Response]:
    def handler(request: httpx.Request) -> httpx.Response:
        raise exc

    return handler


async def check_health_matrix(cls: type[HttpProviderAdapter], ok_body: Any) -> None:
    cases = [
        (lambda r: json_response(200, ok_body), HealthStatus.HEALTHY),
        (lambda r: json_response(401, {"error": {"message": "no"}}), HealthStatus.DOWN),
        (lambda r: json_response(429, {"error": {"message": "slow"}}), HealthStatus.DEGRADED),
        (lambda r: json_response(503, {"error": {"message": "down"}}), HealthStatus.DOWN),
        (raiser(httpx.ConnectError("boom")), HealthStatus.DOWN),
        (lambda r: httpx.Response(200, content=b"<html>"), HealthStatus.DEGRADED),
    ]
    for handler, expected in cases:
        adapter, _ = make(cls, handler, key=SECRET)
        async with adapter:
            assert (await adapter.health_check()).status == expected
    adapter, rec = make(cls, lambda r: json_response(200, ok_body), key=None)
    async with adapter:
        h = await adapter.health_check()
    assert h.status == HealthStatus.DOWN
    assert ENV_NAME in (h.detail or "")
    assert rec.requests == []


async def check_secret_safety(cls: type[HttpProviderAdapter], caplog) -> None:
    caplog.set_level(logging.DEBUG)

    def leak(request: httpx.Request) -> httpx.Response:
        return json_response(401, {"error": {"message": f"Incorrect API key provided: {SECRET}"}})

    adapter, _ = make(cls, leak, key=SECRET)
    async with adapter:
        try:
            await adapter._get_json(cls.HEALTH_PATH)
        except ProviderError as err:
            assert SECRET not in str(err)
            assert SECRET not in repr(err)
            assert SECRET not in "".join(traceback.format_exception(err))
            assert err.__cause__ is None
            assert err.__context__ is None
        else:
            raise AssertionError("expected ProviderError")
        assert SECRET not in repr(adapter)

    adapter, _ = make(cls, raiser(httpx.ConnectError(f"cannot reach {SECRET}")), key=SECRET)
    async with adapter:
        try:
            await adapter._get_json(cls.HEALTH_PATH)
        except ProviderError as err:
            assert SECRET not in str(err)
            assert SECRET not in "".join(traceback.format_exception(err))
            assert err.__cause__ is None
            assert err.__context__ is None
        else:
            raise AssertionError("expected ProviderError")
    assert SECRET not in caplog.text


async def check_missing_credential(cls: type[HttpProviderAdapter]) -> None:
    adapter, rec = make(cls, lambda r: json_response(200, {}), key=None)
    async with adapter:
        try:
            await adapter._get_json(cls.HEALTH_PATH)
        except MissingCredentialError as err:
            assert ENV_NAME in str(err)
        else:
            raise AssertionError("expected MissingCredentialError")
    assert rec.requests == []


async def check_errors(cls: type[HttpProviderAdapter], path_call: str = "list_models") -> list:
    """429 with Retry-After, 401, 5xx, timeouts; returns nothing but asserts."""
    from app.providers.base import ErrorClass

    async def run(handler):
        adapter, _ = make(cls, handler, key=SECRET)
        async with adapter:
            try:
                await getattr(adapter, path_call)()
            except Exception as e:
                return adapter, e
        raise AssertionError("expected error")

    adapter, err = await run(
        lambda r: json_response(429, {"error": {"message": "slow"}}, {"Retry-After": "12"})
    )
    assert isinstance(err, ProviderError)
    assert err.error_class == ErrorClass.RATE_LIMITED
    assert err.retry_after_s == 12
    assert err.retry_after_ms == 12000
    assert await adapter.classify_error(err) == ErrorClass.RATE_LIMITED

    adapter, err = await run(lambda r: json_response(401, {"error": {"message": "bad"}}))
    assert err.error_class == ErrorClass.AUTH_FAILED

    for status in (500, 502, 503):
        _, err = await run(lambda r, s=status: json_response(s, {}))
        assert err.error_class == ErrorClass.PROVIDER_UNAVAILABLE
    _, err = await run(lambda r: json_response(504, {}))
    assert err.error_class == ErrorClass.TIMEOUT_UNKNOWN_OUTCOME

    _, err = await run(raiser(httpx.ReadTimeout("t")))
    assert err.error_class == ErrorClass.TIMEOUT_UNKNOWN_OUTCOME
    _, err = await run(raiser(httpx.ConnectError("c")))
    assert err.error_class == ErrorClass.TRANSIENT_NETWORK

    # classify_error also accepts an httpx.HTTPStatusError
    resp = json_response(429, {"error": {"message": "x"}})
    resp.request = httpx.Request("GET", "https://x/")
    status_err = httpx.HTTPStatusError("e", request=resp.request, response=resp)
    assert await adapter.classify_error(status_err) == ErrorClass.RATE_LIMITED
    return []
