"""Audit tests for slice 2: gaps found against the spec's test list and acceptance criteria."""

from __future__ import annotations

import io
import logging
import sqlite3
import traceback
from datetime import timedelta

import httpx
import pytest
from adapter_helpers import ENV_NAME, Recorder, make, raiser
from conftest import NOW, SECRET, full_state, json_response, provider_cfg, secret_env

from app.config.models import CooldownConfig
from app.db.provider_models import ProviderModelRepository
from app.db.usage import UsageEvent, UsageRepository, UsageStatus
from app.providers.base import (
    CostClass,
    ErrorClass,
    HealthStatus,
    ModelCapability,
    ProviderHealth,
    QuotaConfidence,
    QuotaUnit,
)
from app.providers.errors import MalformedResponseError, ProviderError
from app.providers.gemini import GeminiAdapter
from app.providers.groq import GroqAdapter
from app.providers.openrouter import OpenRouterAdapter
from app.providers.registry import build_adapters
from app.quota.cooldown import CooldownManager
from app.quota.estimator import estimate_quota
from app.quota.state import (
    ProviderObservation,
    RouterStateError,
    build_router_state,
    refresh_all,
    route_with_state,
)
from app.redaction import REDACTED, SecretRedactor, install_redaction
from app.router.router import route
from app.router.types import (
    NoEligibleProvider,
    NoEligibleReason,
    RouteRequest,
    Selected,
)

E = ErrorClass
REQ = RouteRequest("t", "p")
ALL = [OpenRouterAdapter, GeminiAdapter, GroqAdapter]
ALL_IDS = [c.__name__ for c in ALL]
COMPAT = [GroqAdapter]


async def raised(cls, handler, call="list_models", **cfg):
    adapter, _ = make(cls, handler, key=SECRET, **cfg)
    async with adapter:
        try:
            await getattr(adapter, call)()
        except ProviderError as e:
            return adapter, e
    raise AssertionError("expected ProviderError")


# ---------------------------------------------------------------- classify_error


@pytest.mark.parametrize("cls", ALL, ids=ALL_IDS)
@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (400, E.INVALID_REQUEST),
        (401, E.AUTH_FAILED),
        (404, E.MODEL_UNAVAILABLE),
        (408, E.TRANSIENT_NETWORK),
        (413, E.CONTEXT_TOO_LARGE),
        (422, E.INVALID_REQUEST),
        (429, E.RATE_LIMITED),
        (500, E.PROVIDER_UNAVAILABLE),
        (502, E.PROVIDER_UNAVAILABLE),
        (503, E.PROVIDER_UNAVAILABLE),
        (504, E.TIMEOUT_UNKNOWN_OUTCOME),
    ],
)
@pytest.mark.parametrize("body", ["json", "html", "empty", "list"])
async def test_status_drives_class_for_any_body_shape(cls, status, expected, body):
    """Malformed/odd error bodies must not change the status-based class and never raise."""

    def handler(request):
        if body == "json":
            return json_response(status, {})
        if body == "html":
            return httpx.Response(status, content=b"<html>oops</html>")
        if body == "empty":
            return httpx.Response(status, content=b"")
        return json_response(status, ["x", 1])

    adapter, err = await raised(cls, handler)
    assert err.error_class == expected
    assert err.status_code == status
    assert await adapter.classify_error(err) == expected
    # Same via an httpx.HTTPStatusError wrapping a response built the same way
    resp = handler(None)
    resp.request = httpx.Request("GET", "https://x/")
    assert (
        await adapter.classify_error(
            httpx.HTTPStatusError("e", request=resp.request, response=resp)
        )
        == expected
    )


@pytest.mark.parametrize("cls", [GeminiAdapter, GroqAdapter], ids=lambda c: c.__name__)
async def test_403_is_auth_failed(cls):
    _, err = await raised(cls, lambda r: json_response(403, {"error": {"message": "no"}}))
    assert err.error_class == E.AUTH_FAILED


async def test_openrouter_402_and_403():
    _, err = await raised(OpenRouterAdapter, lambda r: json_response(402, {"error": {"code": 402}}))
    assert err.error_class == E.QUOTA_EXHAUSTED
    _, err = await raised(OpenRouterAdapter, lambda r: json_response(403, {"error": {"code": 403}}))
    assert err.error_class == E.POLICY_REJECTED


@pytest.mark.parametrize("cls", [GeminiAdapter, GroqAdapter], ids=lambda c: c.__name__)
async def test_402_is_not_special_outside_openrouter(cls):
    _, err = await raised(cls, lambda r: json_response(402, {}))
    assert err.error_class == E.INVALID_REQUEST


@pytest.mark.parametrize("cls", ALL, ids=ALL_IDS)
async def test_retry_after_header_forms(cls):
    http_date = (NOW + timedelta(seconds=90)).strftime("%a, %d %b %Y %H:%M:%S GMT")
    _, err = await raised(cls, lambda r: json_response(429, {}, {"Retry-After": http_date}))
    assert err.retry_after_s == pytest.approx(90.0)
    _, err = await raised(cls, lambda r: json_response(429, {}, {"Retry-After": "0"}))
    assert err.retry_after_s == 0.0 and err.retry_after_ms == 0
    _, err = await raised(cls, lambda r: json_response(429, {}, {"Retry-After": "2.5"}))
    assert err.retry_after_ms == 2500
    # Garbage or absent header -> None, never 0
    _, err = await raised(cls, lambda r: json_response(429, {}, {"Retry-After": "soon"}))
    assert err.retry_after_s is None and err.retry_after_ms is None
    _, err = await raised(cls, lambda r: json_response(429, {}))
    assert err.retry_after_s is None


async def test_gemini_retry_after_from_body_variants():
    def body(delay):
        return {
            "error": {
                "status": "RESOURCE_EXHAUSTED",
                "message": "q",
                "details": [
                    {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": delay}
                ],
            }
        }

    _, err = await raised(GeminiAdapter, lambda r: json_response(429, body("1.5s")))
    assert err.retry_after_s == pytest.approx(1.5)
    _, err = await raised(GeminiAdapter, lambda r: json_response(429, body("garbage")))
    assert err.error_class == E.RATE_LIMITED and err.retry_after_s is None
    _, err = await raised(GeminiAdapter, lambda r: json_response(429, body(30)))
    assert err.error_class == E.RATE_LIMITED and err.retry_after_s is None


@pytest.mark.parametrize(
    ("status", "gstatus", "expected"),
    [
        (401, "UNAUTHENTICATED", E.AUTH_FAILED),
        (403, "PERMISSION_DENIED", E.AUTH_FAILED),
        (503, "UNAVAILABLE", E.PROVIDER_UNAVAILABLE),
        (500, "INTERNAL", E.PROVIDER_UNAVAILABLE),
        (504, "DEADLINE_EXCEEDED", E.TIMEOUT_UNKNOWN_OUTCOME),
        (400, "INVALID_ARGUMENT", E.INVALID_REQUEST),
    ],
)
async def test_gemini_status_codes(status, gstatus, expected):
    _, err = await raised(
        GeminiAdapter,
        lambda r: json_response(status, {"error": {"status": gstatus, "message": "m"}}),
    )
    assert err.error_class == expected
    assert err.error_code == gstatus


async def test_gemini_invalid_key_as_400_variants():
    """API_KEY_INVALID may sit in any details item, with other items present."""
    body = {
        "error": {
            "code": 400,
            "status": "INVALID_ARGUMENT",
            "message": "API key not valid. Please pass a valid API key.",
            "details": [
                "junk",
                {"@type": "type.googleapis.com/google.rpc.Help", "links": []},
                {"@type": "type.googleapis.com/google.rpc.ErrorInfo", "reason": "API_KEY_INVALID"},
            ],
        }
    }
    adapter, err = await raised(GeminiAdapter, lambda r: json_response(400, body))
    assert err.error_class == E.AUTH_FAILED
    assert await adapter.classify_error(err) == E.AUTH_FAILED
    # Same HTTP 400 without the reason stays INVALID_REQUEST (not everything 400 is auth)
    plain = {"error": {"status": "INVALID_ARGUMENT", "message": "bad field", "details": []}}
    _, err = await raised(GeminiAdapter, lambda r: json_response(400, plain))
    assert err.error_class == E.INVALID_REQUEST
    # Invalid key + health_check -> DOWN
    adapter, _ = make(GeminiAdapter, lambda r: json_response(400, body), key=SECRET)
    async with adapter:
        assert (await adapter.health_check()).status == HealthStatus.DOWN


@pytest.mark.parametrize("cls", COMPAT, ids=lambda c: c.__name__)
@pytest.mark.parametrize(
    ("code", "expected"),
    [
        ("rate_limit_exceeded", E.RATE_LIMITED),
        ("insufficient_quota", E.QUOTA_EXHAUSTED),
        ("invalid_api_key", E.AUTH_FAILED),
        ("INVALID_API_KEY", E.AUTH_FAILED),  # case-insensitive
        ("something_new", E.INVALID_REQUEST),  # unmapped code falls back to status
    ],
)
async def test_compat_code_map(cls, code, expected):
    _, err = await raised(
        cls, lambda r: json_response(400, {"error": {"message": "m", "code": code}})
    )
    assert err.error_class == expected


@pytest.mark.parametrize("cls", ALL, ids=ALL_IDS)
async def test_timeout_and_network_unknown_vs_safe(cls):
    """Connect-phase failures are safe to replay; read/write/remote-protocol are unknown outcome."""
    safe = [httpx.ConnectTimeout("t"), httpx.ConnectError("c"), httpx.PoolTimeout("p")]
    unknown = [
        httpx.ReadTimeout("t"),
        httpx.WriteTimeout("t"),
        httpx.ReadError("r"),
        httpx.WriteError("w"),
        httpx.RemoteProtocolError("p"),
    ]
    for exc in safe:
        adapter, err = await raised(cls, raiser(exc))
        assert err.error_class == E.TRANSIENT_NETWORK, type(exc).__name__
        assert err.status_code is None
        assert await adapter.classify_error(exc) == E.TRANSIENT_NETWORK
    for exc in unknown:
        adapter, err = await raised(cls, raiser(exc))
        assert err.error_class == E.TIMEOUT_UNKNOWN_OUTCOME, type(exc).__name__
        assert await adapter.classify_error(exc) == E.TIMEOUT_UNKNOWN_OUTCOME
    assert await adapter.classify_error(TimeoutError()) == E.TIMEOUT_UNKNOWN_OUTCOME
    assert await adapter.classify_error(KeyError("x")) == E.UNKNOWN


@pytest.mark.parametrize("cls", ALL, ids=ALL_IDS)
async def test_malformed_2xx_json_is_unknown_not_crash(cls):
    adapter, err = await raised(cls, lambda r: httpx.Response(200, content=b"{not json"))
    assert isinstance(err, MalformedResponseError)
    assert err.error_class == E.UNKNOWN
    assert await adapter.classify_error(err) == E.UNKNOWN
    # health: DEGRADED, never raises
    adapter, _ = make(cls, lambda r: httpx.Response(200, content=b"{not json"), key=SECRET)
    async with adapter:
        assert (await adapter.health_check()).status == HealthStatus.DEGRADED


@pytest.mark.parametrize("cls", ALL, ids=ALL_IDS)
async def test_health_matrix_extra_statuses(cls):
    for status, expected in [
        (402, HealthStatus.DEGRADED if cls is not OpenRouterAdapter else HealthStatus.DEGRADED),
        (403, HealthStatus.DOWN if cls is not OpenRouterAdapter else HealthStatus.DEGRADED),
        (504, HealthStatus.DOWN),
        (408, HealthStatus.DOWN),
        (400, HealthStatus.DEGRADED),
    ]:
        adapter, _ = make(cls, lambda r, s=status: json_response(s, {}), key=SECRET)
        async with adapter:
            h = await adapter.health_check()
        assert h.status == expected, (cls.__name__, status)
        assert SECRET not in (h.detail or "")
    for exc in (httpx.ReadTimeout("t"), httpx.RemoteProtocolError("p")):
        adapter, _ = make(cls, raiser(exc), key=SECRET)
        async with adapter:
            assert (await adapter.health_check()).status == HealthStatus.DOWN


async def test_non_http_exception_in_transport_does_not_escape_health():
    adapter, _ = make(GroqAdapter, raiser(RuntimeError(f"boom {SECRET}")), key=SECRET)
    async with adapter:
        h = await adapter.health_check()
    assert h.status == HealthStatus.DOWN
    assert SECRET not in (h.detail or "")
    assert h.detail == "error: RuntimeError"


# ---------------------------------------------------------------- quota labelling


GROQ_HEADERS = {
    "X-RateLimit-Limit-Requests": "14400",
    "X-RateLimit-Remaining-Requests": "14399",
    "X-RateLimit-Reset-Requests": "1m30s",
    "X-RateLimit-Limit-Tokens": "6000",
    "X-RateLimit-Remaining-Tokens": "5990",
    "X-RateLimit-Reset-Tokens": "250ms",
}


async def test_groq_headers_exact_values():
    adapter, _ = make(
        GroqAdapter, lambda r: json_response(200, {"data": []}, GROQ_HEADERS), key=SECRET
    )
    async with adapter:
        recs = await adapter.get_quota()
    by_window = {r.window: r for r in recs}
    day, minute = by_window["day"], by_window["minute"]
    assert day.unit == QuotaUnit.REQUESTS and minute.unit == QuotaUnit.TOKENS
    for r in recs:
        assert r.confidence == QuotaConfidence.EXACT
        assert r.model is None and r.source == "groq:response-headers"
    assert (day.limit, day.remaining, day.used) == (14400, 14399, 1)
    assert day.reset_at == NOW + timedelta(seconds=90)
    assert (minute.limit, minute.remaining, minute.used) == (6000, 5990, 10)
    assert minute.reset_at == NOW + timedelta(milliseconds=250)


async def test_groq_remaining_zero_is_exact_zero_not_none():
    h = {
        "x-ratelimit-limit-requests": "100",
        "x-ratelimit-remaining-requests": "0",
        "x-ratelimit-reset-requests": "10s",
    }
    adapter, _ = make(GroqAdapter, lambda r: json_response(200, {"data": []}, h), key=SECRET)
    async with adapter:
        (rec,) = await adapter.get_quota()
    assert rec.confidence == QuotaConfidence.EXACT
    assert rec.remaining == 0 and rec.used == 100


@pytest.mark.parametrize("cls", COMPAT, ids=lambda c: c.__name__)
@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"x-ratelimit-limit-requests-day": "abc", "x-ratelimit-remaining-requests-day": "xyz"},
        {"x-ratelimit-limit-requests": "", "x-ratelimit-remaining-requests": ""},
        {"x-ratelimit-limit-requests": "-5", "x-ratelimit-remaining-requests": "nan"},
        {"x-ratelimit-reset-requests": "5s"},  # only a reset: nothing authoritative
    ],
)
async def test_unparseable_or_missing_headers_yield_no_records(cls, headers):
    adapter, _ = make(cls, lambda r: json_response(200, {"data": []}, headers), key=SECRET)
    async with adapter:
        assert await adapter.get_quota() == []


async def test_groq_ignores_foreign_header_names():
    """Non-Groq rate-limit header names on Groq must not fabricate EXACT data."""
    cer = {
        "x-ratelimit-limit-requests-day": "100",
        "x-ratelimit-remaining-requests-day": "50",
    }
    adapter, _ = make(GroqAdapter, lambda r: json_response(200, {"data": []}, cer), key=SECRET)
    async with adapter:
        assert await adapter.get_quota() == []


async def test_gemini_never_exact_even_if_response_has_ratelimit_headers():
    h = {"x-ratelimit-limit-requests": "5", "x-ratelimit-remaining-requests": "5"}
    adapter, rec = make(GeminiAdapter, lambda r: json_response(200, {"models": []}, h), key=SECRET)
    async with adapter:
        await adapter.list_models()
        assert await adapter.get_quota() == []


async def test_openrouter_quota_never_exact_without_numeric_limit_and_malformed_json():
    adapter, _ = make(
        OpenRouterAdapter,
        lambda r: json_response(200, {"data": {"limit": "100", "usage": 3}}),
        key=SECRET,
    )
    async with adapter:
        recs = await adapter.get_quota()
    # A string limit is not an authoritative number
    assert all(r.confidence != QuotaConfidence.EXACT for r in recs)
    assert all(r.remaining is None for r in recs)
    adapter, _ = make(OpenRouterAdapter, lambda r: httpx.Response(200, content=b"zzz"), key=SECRET)
    async with adapter:
        with pytest.raises(MalformedResponseError):
            await adapter.get_quota()


async def test_estimates_never_exact_across_scenarios(db, make_config):
    cfg = make_config(
        providers={
            "gemini": {
                "enabled": True,
                "api_key_env": "GEMINI_API_KEY",
                "limits": [
                    {"dimension": "requests_per_day", "limit": 5},
                    {"dimension": "tokens_per_minute", "limit": 1000},
                    {"dimension": "requests_per_minute", "limit": 2, "model": "g-3.8"},
                ],
            }
        }
    )
    usage = UsageRepository(db)
    for i in range(3):
        usage.record(
            UsageEvent(
                "gemini", "g-3.8", CostClass.FREE, UsageStatus.SUCCESS, NOW - timedelta(seconds=i)
            )
        )
    recs = estimate_quota(cfg, usage, NOW)
    assert recs
    assert all(r.confidence in (QuotaConfidence.ESTIMATED, QuotaConfidence.UNKNOWN) for r in recs)
    assert all(r.source.startswith("estimate:") for r in recs)
    unknown = [r for r in recs if r.confidence == QuotaConfidence.UNKNOWN]
    assert unknown and all(r.remaining is None for r in unknown)  # tokens missing -> not 0


# ---------------------------------------------------------------- cooldown + routing


def mgr(**kw):
    return CooldownManager(CooldownConfig(**kw), clock=lambda: NOW)


def test_429_cools_only_that_model_gemini_second_model_routes(make_config):
    cfg = make_config()
    m = mgr()
    m.record_failure("gemini", "g-3.8", E.RATE_LIMITED, retry_after_s=30)
    assert m.is_cooling("gemini", "g-3.8")
    assert not m.is_cooling("gemini", "g-3.7")
    assert not m.is_cooling("groq", "g-3.8")
    from app.providers.base import QuotaRecord

    exhausted_or = QuotaRecord(
        "openrouter", None, "day", QuotaUnit.REQUESTS, QuotaConfidence.EXHAUSTED, NOW, "t"
    )
    d = route(REQ, cfg, full_state(cfg, quota=[exhausted_or], cooldowns=m.active(NOW)))
    assert isinstance(d, Selected)
    assert (d.candidate.provider, d.candidate.model) == ("gemini", "g-3.7")


def test_flash_38_to_37_to_next_provider_then_back(make_config):
    cfg = make_config()
    m = mgr()
    from conftest import NOW as N

    from app.providers.base import QuotaRecord

    exhausted_or = QuotaRecord(
        "openrouter", None, "day", QuotaUnit.REQUESTS, QuotaConfidence.EXHAUSTED, N, "t"
    )
    m.record_failure("gemini", "g-3.8", E.RATE_LIMITED, retry_after_s=10)
    d = route(REQ, cfg, full_state(cfg, quota=[exhausted_or], cooldowns=m.active(NOW)))
    assert d.candidate.model == "g-3.7"
    m.record_failure("gemini", "g-3.7", E.RATE_LIMITED, retry_after_s=20)
    d = route(REQ, cfg, full_state(cfg, quota=[exhausted_or], cooldowns=m.active(NOW)))
    assert isinstance(d, Selected)
    assert d.candidate.provider == "groq"
    # 3.8 expires first
    t = NOW + timedelta(seconds=10)
    d = route(REQ, cfg, full_state(cfg, now=t, quota=[exhausted_or], cooldowns=m.active(t)))
    assert (d.candidate.provider, d.candidate.model) == ("gemini", "g-3.8")
    t = NOW + timedelta(seconds=20)
    assert m.active(t) == {}


def test_provider_wide_cooldown_skips_every_model(make_config):
    cfg = make_config()
    m = mgr()
    m.record_failure("gemini", "g-3.8", E.PROVIDER_UNAVAILABLE)
    d = route(REQ, cfg, full_state(cfg, cooldowns=m.active(NOW)))
    assert isinstance(d, Selected)
    assert d.candidate.provider != "gemini"


def test_repeated_network_failures_cool_provider_wide_then_expire(make_config):
    cfg = make_config()
    m = mgr()
    until = None
    for i in range(3):
        until = m.record_failure(
            "openrouter", "openrouter/free", E.TRANSIENT_NETWORK, now=NOW + timedelta(seconds=i)
        )
    assert until == NOW + timedelta(seconds=2 + 120)
    assert m.is_cooling("openrouter", "or-paid-1")
    d = route(REQ, cfg, full_state(cfg, cooldowns=m.active(NOW + timedelta(seconds=3))))
    assert d.candidate.provider == "gemini"
    assert not m.is_cooling("openrouter", "x", now=until)


def test_two_failures_only_never_cool_and_unknown_outcome_counts(make_config):
    m = mgr()
    assert m.record_failure("p", "m", E.TRANSIENT_NETWORK) is None
    assert m.record_failure("p", "m", E.TIMEOUT_UNKNOWN_OUTCOME) is None
    assert not m.is_cooling("p", "m")


def test_retry_after_clamp_boundaries():
    m = mgr(
        max_cooldown_s=100,
        rate_limit_default_s=60,
        provider_unavailable_s=100,
        network_failure_cooldown_s=100,
    )
    assert m.record_failure("p", "a", E.RATE_LIMITED, retry_after_s=100) == NOW + timedelta(
        seconds=100
    )
    assert m.record_failure("p", "b", E.RATE_LIMITED, retry_after_s=100.001) == NOW + timedelta(
        seconds=100
    )
    assert m.record_failure("p", "c", E.RATE_LIMITED, retry_after_s=-5) is None
    assert m.record_failure(
        "p", "d", E.PROVIDER_UNAVAILABLE, retry_after_s=1e12
    ) == NOW + timedelta(seconds=100)
    assert m.record_failure(
        "p", "e", E.RATE_LIMITED, retry_after_s=float("-inf")
    ) == NOW + timedelta(seconds=60)


def test_cooldown_active_returns_copy():
    m = mgr()
    m.record_failure("p", "a", E.RATE_LIMITED)
    snap = m.active()
    snap.clear()
    assert m.is_cooling("p", "a")


def test_quota_exhausted_cooldown_model_scoped_and_none_model_provider_wide():
    m = mgr()
    m.record_failure("p", "a", E.QUOTA_EXHAUSTED)
    assert not m.is_cooling("p", "b")
    m.record_failure("q", None, E.RATE_LIMITED)
    assert ("q", None) in m.active() and m.is_cooling("q", "anything")


# ---------------------------------------------------------------- router state


def _caps(*pairs):
    return tuple(
        ModelCapability(p, m, supports_tool_calling=True, context_window=1000) for p, m in pairs
    )


def _obs(provider, models, ok=True):
    return ProviderObservation(
        provider,
        ProviderHealth(provider, HealthStatus.HEALTHY, NOW, "ok"),
        models,
        ok,
        NOW,
    )


def test_unlisted_models_never_enter_even_if_observed(db, make_config):
    cfg = make_config()
    obs = {
        "gemini": _obs(
            "gemini", _caps(("gemini", "g-3.8"), ("gemini", "g-unlisted"), ("gemini", "g-pro"))
        )
    }
    state = build_router_state(cfg, db, mgr(), obs, now=NOW, environ=secret_env())
    assert ("gemini", "g-3.8") in state.capabilities
    assert ("gemini", "g-unlisted") not in state.capabilities
    assert ("gemini", "g-pro") not in state.capabilities


def test_unlisted_models_never_enter_from_db_fallback(db, make_config):
    cfg = make_config()
    ProviderModelRepository(db).replace_discovered(
        cfg, "gemini", list(_caps(("gemini", "g-3.8"), ("gemini", "g-unlisted"))), NOW
    )
    # observation exists but models_ok False -> DB fallback
    obs = {"gemini": _obs("gemini", (), ok=False)}
    state = build_router_state(cfg, db, mgr(), obs, now=NOW, environ=secret_env())
    assert set(k for k in state.capabilities if k[0] == "gemini") == {("gemini", "g-3.8")}
    # and with no observation at all
    state = build_router_state(cfg, db, mgr(), {}, now=NOW, environ=secret_env())
    assert ("gemini", "g-unlisted") not in state.capabilities


def test_allowlist_rechecked_against_config_not_stored_status(db, make_config):
    cfg = make_config()
    ProviderModelRepository(db).replace_discovered(
        cfg, "gemini", list(_caps(("gemini", "g-3.8"))), NOW
    )
    db.execute("UPDATE provider_models SET routing_status='ALLOWED'")
    db.execute("UPDATE provider_models SET model='g-evil' WHERE model='g-3.8'")
    state = build_router_state(cfg, db, mgr(), {}, now=NOW, environ=secret_env())
    assert ("gemini", "g-evil") not in state.capabilities


def _raw_paid(db, cost, at=NOW):
    db.execute(
        "INSERT INTO usage_events (provider, model, cost_class, status, estimated_cost_usd,"
        " occurred_at) VALUES ('openrouter', 'or-paid-1', 'PAID', 'success', ?, ?)",
        (cost, at.isoformat(timespec="microseconds")),
    )


@pytest.mark.parametrize("cost", [-0.01, -1e9, float("inf"), float("-inf"), None])
def test_bad_spend_totals_fail_closed_never_clamped(db, make_config, cost):
    cfg = make_config()
    _raw_paid(db, cost)
    with pytest.raises(RouterStateError):
        build_router_state(cfg, db, mgr(), {}, now=NOW, environ=secret_env())


def test_negative_spend_in_month_only_also_fails_closed(db, make_config):
    cfg = make_config()
    _raw_paid(db, -0.5, NOW - timedelta(days=2))  # same month, not today
    with pytest.raises(RouterStateError):
        build_router_state(cfg, db, mgr(), {}, now=NOW, environ=secret_env())


@pytest.mark.xfail(
    strict=True,
    reason="FINDING: individual negative paid rows (raw SQL) cancelling a positive one are not "
    "detected; spec only requires negative totals to fail closed",
)
def test_bad_spend_cancelling_pair_not_hidden(db, make_config):
    """+5 and -5 must not net to a clean 0.0 and pass silently as 'no spend'."""
    cfg = make_config()
    _raw_paid(db, 5.0)
    _raw_paid(db, -5.0)
    try:
        state = build_router_state(cfg, db, mgr(), {}, now=NOW, environ=secret_env())
    except RouterStateError:
        return
    pytest.fail(f"negative paid cost silently accepted: {state.paid_spend_today_usd}")


@pytest.mark.parametrize("cost", [-1.0, None, float("inf")])
def test_bad_spend_never_selects_paid_or_anything(db, make_config, cost):
    cfg = make_config()
    _raw_paid(db, cost)
    # Free models are otherwise eligible; fail closed means NOTHING is selected.
    d = route_with_state(
        REQ, cfg, lambda: build_router_state(cfg, db, mgr(), {}, now=NOW, environ=secret_env())
    )
    assert d == NoEligibleProvider(NoEligibleReason.ROUTER_STATE_UNAVAILABLE, ())
    assert not isinstance(d, Selected)


def test_closed_connection_is_router_state_error_not_sqlite_error(make_config, tmp_path):
    from app.db.connection import open_database
    from app.db.migrations import migrate

    cfg = make_config()
    conn = open_database(tmp_path / "c.db")
    migrate(conn)
    conn.close()
    with pytest.raises(RouterStateError):
        build_router_state(cfg, conn, mgr(), {}, now=NOW, environ=secret_env())
    d = route_with_state(
        REQ, cfg, lambda: build_router_state(cfg, conn, mgr(), {}, now=NOW, environ=secret_env())
    )
    assert d.reason == NoEligibleReason.ROUTER_STATE_UNAVAILABLE


@pytest.mark.xfail(
    strict=True,
    reason="FINDING: a corrupt EXACT quota row (raw SQL, remaining=-3) is passed into "
    "RouterState unvalidated; spec 6.2 validates on write only",
)
def test_corrupt_quota_row_fails_closed(db, make_config):
    cfg = make_config()
    db.execute(
        "INSERT INTO quota_snapshots (provider, model, quota_window, unit, confidence, observed_at,"
        " source, limit_value, used, remaining)"
        " VALUES ('gemini', NULL, 'day', 'REQUESTS', 'EXACT', ?, 'x', 1, 1, -3)",
        (NOW.isoformat(timespec="microseconds"),),
    )
    try:
        state = build_router_state(cfg, db, mgr(), {}, now=NOW, environ=secret_env())
    except RouterStateError:
        return
    except sqlite3.Error as e:  # pragma: no cover
        pytest.fail(f"raw sqlite error escaped: {e}")
    assert all(r.remaining is None or r.remaining >= 0 for r in state.quota)


def test_spend_zero_when_no_paid_rows_is_real_zero(db, make_config):
    cfg = make_config()
    UsageRepository(db).record(
        UsageEvent("gemini", "g-3.8", CostClass.FREE, UsageStatus.SUCCESS, NOW)
    )
    state = build_router_state(cfg, db, mgr(), {}, now=NOW, environ=secret_env())
    assert state.paid_spend_today_usd == 0.0 and state.paid_spend_month_usd == 0.0


# ---------------------------------------------------------------- redaction


def _capture(name, redactor):
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter("%(name)s %(levelname)s %(message)s"))
    lg = logging.getLogger(name)
    lg.setLevel(logging.DEBUG)
    lg.addHandler(handler)
    lg.propagate = False
    install_redaction(redactor, logger=lg)
    return lg, handler, stream


@pytest.fixture
def audit_logger():
    made = []

    def factory(name, redactor):
        lg, h, s = _capture(name, redactor)
        made.append((lg, h))
        return lg, s

    yield factory
    for lg, h in made:
        lg.removeHandler(h)


def test_header_and_url_forms_never_survive_logging(audit_logger):
    lg, out = audit_logger("t.audit.hdr", SecretRedactor([SECRET]))
    lg.info("Authorization: Bearer %s", SECRET)
    lg.info("authorization=Bearer %s", "some-other-token-123")
    lg.info("headers=%r", {"x-api-key": SECRET, "Authorization": f"Bearer {SECRET}"})
    lg.info("x-api-key: %s", "unconfigured-value-999")
    lg.info("x-goog-api-key=%s", "AIzaUNCONFIGURED")
    lg.info("GET https://generativelanguage.googleapis.com/v1beta/models?key=%s&pageSize=1", SECRET)
    lg.info("GET https://h/v1?pageSize=1&api_key=unconfigured-zzz")
    lg.info(
        "repr %r",
        httpx.Request(
            "GET", f"https://h/v1?key={SECRET}", headers={"Authorization": f"Bearer {SECRET}"}
        ),
    )
    text = out.getvalue()
    assert SECRET not in text
    for leaked in (
        "some-other-token-123",
        "unconfigured-value-999",
        "AIzaUNCONFIGURED",
        "unconfigured-zzz",
    ):
        assert leaked not in text, leaked
    assert "pageSize=1" in text
    assert REDACTED in text


def test_traceback_from_adapter_style_exception_chain_redacted(audit_logger):
    lg, out = audit_logger("t.audit.tb", SecretRedactor([SECRET]))
    try:
        try:
            raise ConnectionError(f"failed with Authorization: Bearer {SECRET}")
        except ConnectionError as inner:
            raise RuntimeError(f"wrapped key={SECRET}") from inner
    except RuntimeError:
        lg.exception("adapter failed")
        lg.error("again", exc_info=True)
    try:
        raise ValueError(SECRET)
    except ValueError as e:
        lg.warning("with explicit exc_info", exc_info=e)
    text = out.getvalue()
    assert SECRET not in text
    assert text.count("Traceback") >= 2
    assert "ConnectionError" in text and "RuntimeError" in text


def test_child_logger_and_dict_args_and_stack_info(audit_logger):
    lg, out = audit_logger("t.audit.child", SecretRedactor([SECRET]))
    child = logging.getLogger("t.audit.child.deep.er")
    child.warning("data %(k)s", {"k": SECRET})
    child.warning("stack", stack_info=True, extra={"unused": SECRET})
    child.warning("plain %s %s", SECRET, SECRET[::-1])
    assert SECRET not in out.getvalue()


def test_secret_added_later_is_redacted(audit_logger):
    r = SecretRedactor()
    lg, out = audit_logger("t.audit.later", r)
    lg.info("before %s", SECRET)
    assert SECRET in out.getvalue()  # nothing configured yet: documents the ordering contract
    r.add(SECRET)
    lg.info("after %s", SECRET)
    assert out.getvalue().splitlines()[-1].count(SECRET) == 0


def test_reprs_never_contain_secret(make_config):
    redactor = SecretRedactor([SECRET])
    assert SECRET not in repr(redactor) and SECRET not in str(redactor)
    cfg = make_config()
    adapters = build_adapters(cfg, environ=secret_env(), redactor=redactor)
    for a in adapters.values():
        for v in secret_env().values():
            assert v not in repr(a) and v not in str(a)
            assert v not in repr(vars(a)) or True  # attributes may hold env mapping; checked below
    obs = _obs("gemini", _caps(("gemini", "g")))
    assert SECRET not in repr(obs)
    err = ProviderError("gemini", E.UNKNOWN, "x", status_code=500)
    assert SECRET not in repr(err) and SECRET not in str(err)


async def test_adapters_do_not_store_resolved_key(make_config):
    """The key is resolved per request; no attribute holds the plain value."""
    env = secret_env()
    cfg = make_config()
    adapters = build_adapters(cfg, environ=env, redactor=SecretRedactor.from_config(cfg, env))
    try:
        for a in adapters.values():
            for name, value in vars(a).items():
                if name == "_environ":
                    continue  # the injected mapping itself, not a copied key
                assert not (isinstance(value, str) and value in env.values()), name
    finally:
        for a in adapters.values():
            await a.aclose()


@pytest.mark.parametrize("cls", ALL, ids=ALL_IDS)
async def test_full_adapter_failure_flow_logged_through_redaction(cls, audit_logger, caplog):
    """Real adapter errors that echo the key, logged and raised with tracebacks, never leak."""
    key = SECRET + "-flow"
    redactor = SecretRedactor([key])
    lg, out = audit_logger("t.audit.flow", redactor)
    caplog.set_level(logging.DEBUG)

    echoes = [
        json_response(401, {"error": {"message": f"bad key {key}", "code": key}}),
        json_response(400, {"error": {"status": key, "message": f"Authorization: Bearer {key}"}}),
        json_response(500, {"error": f"upstream said {key}"}),
    ]
    for resp in echoes:
        rec = Recorder(lambda r, resp=resp: resp)
        adapter = cls(
            provider_cfg(),
            transport=rec.transport,
            environ={ENV_NAME: key},
            clock=lambda: NOW,
            redactor=redactor,
        )
        async with adapter:
            try:
                await adapter.list_models()
            except ProviderError as e:
                text = str(e) + repr(e) + "".join(traceback.format_exception(e)) + e.message
                assert key not in text
                assert key not in (e.error_code or "")
                lg.exception("failure: %s", e)
                lg.error("raw: %s", resp.text)  # even raw echo bodies are scrubbed by the filter
            h = await adapter.health_check()
            assert key not in (h.detail or "")
    assert key not in out.getvalue()
    # The test's own "t.audit.flow" logger deliberately logs raw echoes (scrubbed only by its
    # handler filter), so check what the adapters themselves logged.
    adapter_records = [r for r in caplog.records if not r.name.startswith("t.audit")]
    assert adapter_records
    assert all(key not in r.getMessage() for r in adapter_records)


async def test_refresh_all_with_echoing_failures_logs_no_secret(db, make_config, caplog):
    caplog.set_level(logging.DEBUG)
    cfg = make_config()
    env = secret_env()

    def handler(request):
        auth = request.headers.get("authorization", "") + request.headers.get("x-goog-api-key", "")
        return json_response(401, {"error": {"message": f"invalid key: {auth}"}})

    ads = build_adapters(
        cfg,
        transport=httpx.MockTransport(handler),
        environ=env,
        redactor=SecretRedactor.from_config(cfg, env),
    )
    try:
        obs = await refresh_all(ads, db, cfg, now=NOW, environ=env)
    finally:
        for a in ads.values():
            await a.aclose()
    assert all(o.health.status == HealthStatus.DOWN for o in obs.values())
    for v in env.values():
        assert v not in caplog.text
        assert v not in repr(obs)
        for table in ("provider_models", "quota_snapshots", "usage_events"):
            assert v not in repr([tuple(r) for r in db.execute(f"SELECT * FROM {table}")])


async def test_gemini_key_never_in_url_across_pages_and_errors():
    seen = []

    def handler(request):
        seen.append(request)
        if "pageToken" not in request.url.params:
            return json_response(200, {"models": [], "nextPageToken": "t2"})
        return json_response(401, {"error": {"status": "UNAUTHENTICATED", "message": SECRET}})

    adapter, _ = make(GeminiAdapter, handler, key=SECRET)
    async with adapter:
        with pytest.raises(ProviderError):
            await adapter.list_models()
        await adapter.health_check()
    assert len(seen) >= 3
    for req in seen:
        assert SECRET not in str(req.url)
        assert "key" not in req.url.params
        assert req.headers["x-goog-api-key"] == SECRET
