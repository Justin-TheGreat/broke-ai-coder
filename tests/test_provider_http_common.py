from __future__ import annotations

from datetime import timedelta

import httpx
import pytest
from conftest import NOW

from app.providers.base import ErrorClass, QuotaConfidence, QuotaDimension, QuotaUnit
from app.providers.http import HttpProviderAdapter, classify_status, classify_transport_error
from app.providers.ratelimit import (
    parse_duration,
    parse_number,
    parse_rate_limit_headers,
    parse_retry_after,
)

E = ErrorClass


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("12.5", 12.5),
        ("2m59.56s", 179.56),
        ("7.66s", 7.66),
        ("1h2m3s", 3723.0),
        ("120ms", 0.12),
        ("0s", 0.0),
        ("  5s ", 5.0),
        ("", None),
        (None, None),
        ("abc", None),
        ("-1s", None),
        ("5x", None),
    ],
)
def test_parse_duration(text, expected):
    got = parse_duration(text)
    if expected is None:
        assert got is None
    else:
        assert got == pytest.approx(expected)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("5", 5),
        ("5.0", 5),
        ("5.5", 5.5),
        ("0", 0),
        ("-1", None),
        ("x", None),
        ("", None),
        (None, None),
        ("inf", None),
        ("nan", None),
    ],
)
def test_parse_number(text, expected):
    got = parse_number(text)
    assert got == expected
    if isinstance(expected, int):
        assert isinstance(got, int)


def test_parse_retry_after():
    assert parse_retry_after("30", NOW) == 30.0
    future = (NOW + timedelta(seconds=90)).strftime("%a, %d %b %Y %H:%M:%S GMT")
    assert parse_retry_after(future, NOW) == pytest.approx(90.0)
    past = (NOW - timedelta(seconds=90)).strftime("%a, %d %b %Y %H:%M:%S GMT")
    assert parse_retry_after(past, NOW) == 0.0
    assert parse_retry_after("soon", NOW) is None
    assert parse_retry_after(None, NOW) is None


DIMS = {"requests": QuotaDimension.REQUESTS_PER_DAY, "tokens": QuotaDimension.TOKENS_PER_MINUTE}


def test_rate_limit_headers_mixed_case():
    headers = {
        "X-RateLimit-Limit-Requests": "14400",
        "x-ratelimit-remaining-requests": "14370",
        "X-RATELIMIT-RESET-REQUESTS": "2m59.56s",
        "x-ratelimit-limit-tokens": "6000",
        "x-ratelimit-remaining-tokens": "5900",
        "x-ratelimit-reset-tokens": "7.66s",
    }
    recs = parse_rate_limit_headers(headers, DIMS, provider="groq", model=None, now=NOW)
    assert [r.window for r in recs] == ["day", "minute"]
    rpd, tpm = recs
    assert rpd.confidence == QuotaConfidence.EXACT
    assert (rpd.limit, rpd.remaining, rpd.used) == (14400, 14370, 30)
    assert rpd.unit == QuotaUnit.REQUESTS
    assert rpd.reset_at == NOW + timedelta(seconds=179.56)
    assert rpd.source == "groq:response-headers"
    assert rpd.model is None
    assert (tpm.limit, tpm.remaining, tpm.used) == (6000, 5900, 100)
    assert tpm.unit == QuotaUnit.TOKENS


def test_rate_limit_headers_missing_remaining_is_unknown():
    headers = {"x-ratelimit-limit-requests": "100"}
    (rec,) = parse_rate_limit_headers(headers, DIMS, provider="groq", model=None, now=NOW)
    assert rec.confidence == QuotaConfidence.UNKNOWN
    assert rec.remaining is None and rec.used is None and rec.reset_at is None


def test_rate_limit_headers_absent():
    assert parse_rate_limit_headers({}, DIMS, provider="groq", model=None, now=NOW) == []


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (400, E.INVALID_REQUEST),
        (401, E.AUTH_FAILED),
        (403, E.AUTH_FAILED),
        (404, E.MODEL_UNAVAILABLE),
        (408, E.TRANSIENT_NETWORK),
        (413, E.CONTEXT_TOO_LARGE),
        (418, E.INVALID_REQUEST),
        (422, E.INVALID_REQUEST),
        (429, E.RATE_LIMITED),
        (500, E.PROVIDER_UNAVAILABLE),
        (502, E.PROVIDER_UNAVAILABLE),
        (503, E.PROVIDER_UNAVAILABLE),
        (504, E.TIMEOUT_UNKNOWN_OUTCOME),
        (507, E.PROVIDER_UNAVAILABLE),
        (302, E.UNKNOWN),
    ],
)
def test_classify_status(status, expected):
    assert classify_status(status) == expected


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (httpx.ConnectTimeout("x"), E.TRANSIENT_NETWORK),
        (httpx.ConnectError("x"), E.TRANSIENT_NETWORK),
        (httpx.PoolTimeout("x"), E.TRANSIENT_NETWORK),
        (httpx.ProxyError("x"), E.TRANSIENT_NETWORK),
        (httpx.ReadTimeout("x"), E.TIMEOUT_UNKNOWN_OUTCOME),
        (httpx.WriteTimeout("x"), E.TIMEOUT_UNKNOWN_OUTCOME),
        (httpx.ReadError("x"), E.TIMEOUT_UNKNOWN_OUTCOME),
        (httpx.WriteError("x"), E.TIMEOUT_UNKNOWN_OUTCOME),
        (httpx.RemoteProtocolError("x"), E.TIMEOUT_UNKNOWN_OUTCOME),
        (TimeoutError("x"), E.TIMEOUT_UNKNOWN_OUTCOME),
        (ValueError("x"), E.UNKNOWN),
    ],
)
def test_classify_transport_error(exc, expected):
    assert classify_transport_error(exc) == expected


class _Hostile(Exception):
    def __str__(self) -> str:
        raise RuntimeError("no str")


async def test_classify_error_never_raises():
    from app.config.models import ProviderConfig

    class A(HttpProviderAdapter):
        DEFAULT_BASE_URL = "https://example.invalid"
        HEALTH_PATH = "x"

    a = A("a", ProviderConfig(api_key_env="K"))
    assert await a.classify_error(ValueError("x")) == E.UNKNOWN
    assert await a.classify_error(_Hostile()) == E.UNKNOWN
    bad = httpx.HTTPStatusError("e", request=None, response=None)  # type: ignore[arg-type]
    assert await a.classify_error(bad) == E.UNKNOWN
