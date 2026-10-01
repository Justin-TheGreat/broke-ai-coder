from __future__ import annotations

import logging
from datetime import timedelta

import httpx
import pytest
from conftest import NOW, SECRET, json_response, secret_env

from app.db.provider_models import ProviderModelRepository
from app.db.quota import QuotaSnapshotRepository
from app.db.usage import UsageEvent, UsageRepository, UsageStatus
from app.providers.base import (
    CostClass,
    ErrorClass,
    HealthStatus,
    QuotaConfidence,
    QuotaRecord,
    QuotaUnit,
)
from app.providers.registry import build_adapters
from app.quota.cooldown import CooldownManager
from app.quota.state import (
    RouterStateError,
    build_router_state,
    refresh_all,
    route_with_state,
)
from app.router.router import next_after_failure, route
from app.router.types import (
    FailedAttempt,
    NoEligibleProvider,
    NoEligibleReason,
    RouteRequest,
    Selected,
    SkipReason,
)

REQ = RouteRequest("t", "p")
H = timedelta(hours=1)


def handler_for(*, gemini_status=200, groq_status=200):
    def handler(request: httpx.Request) -> httpx.Response:
        host = request.url.host
        if host == "generativelanguage.googleapis.com":
            if gemini_status != 200:
                return json_response(gemini_status, {"error": {"status": "INTERNAL"}})
            return json_response(
                200,
                {
                    "models": [
                        {
                            "name": f"models/{m}",
                            "supportedGenerationMethods": ["generateContent"],
                            "inputTokenLimit": 100000,
                        }
                        for m in ("g-3.8", "g-3.7", "g-pro")
                    ]
                },
            )
        if host == "api.groq.com":
            return json_response(groq_status, {"data": [{"id": "q-x", "context_window": 9000}]})
        if host == "openrouter.ai":
            if request.url.path.endswith("/key"):
                return json_response(200, {"data": {"limit": None, "usage": 0}})
            return json_response(200, {"data": [{"id": "openrouter/free"}, {"id": "or-paid-1"}]})
        raise AssertionError(f"unexpected host {request.url.host}")

    return handler


def adapters(cfg, **kw):
    return build_adapters(
        cfg,
        transport=httpx.MockTransport(handler_for(**kw)),
        environ=secret_env(),
        clock=lambda: NOW,
    )


def cooldowns():
    from app.config.models import CooldownConfig

    return CooldownManager(CooldownConfig(), clock=lambda: NOW)


async def refresh(cfg, db, **kw):
    ads = adapters(cfg, **kw)
    try:
        return await refresh_all(ads, db, cfg, now=NOW, environ=secret_env())
    finally:
        for a in ads.values():
            await a.aclose()


def exhausted_openrouter():
    return QuotaRecord(
        "openrouter", None, "day", QuotaUnit.REQUESTS, QuotaConfidence.EXHAUSTED, NOW, "t"
    )


async def test_end_to_end(db, make_config):
    cfg = make_config()
    obs = await refresh(cfg, db)
    assert set(obs) == {"openrouter", "gemini", "groq"}
    QuotaSnapshotRepository(db).insert(exhausted_openrouter())
    cd = cooldowns()
    cd.record_failure("gemini", "g-3.8", ErrorClass.RATE_LIMITED)
    state = build_router_state(cfg, db, cd, obs, now=NOW, environ=secret_env())
    d = route(REQ, cfg, state)
    assert isinstance(d, Selected)
    assert (d.candidate.provider, d.candidate.model) == ("gemini", "g-3.7")
    status = {
        r["model"]: r["routing_status"]
        for r in db.execute("SELECT * FROM provider_models WHERE provider='gemini'")
    }
    assert status["g-pro"] == "DISCOVERED_ONLY" and status["g-3.8"] == "ALLOWED"
    assert ("gemini", "g-pro") not in state.capabilities
    assert ("gemini", "g-3.7") in state.capabilities
    assert state.health["gemini"] == HealthStatus.HEALTHY


async def test_missing_credentials_not_refreshed(db, make_config):
    cfg = make_config()
    ads = adapters(cfg)
    env = secret_env()
    del env["GROQ_API_KEY"]
    try:
        obs = await refresh_all(ads, db, cfg, now=NOW, environ=env)
    finally:
        for a in ads.values():
            await a.aclose()
    assert "groq" not in obs


async def test_health_down(db, make_config):
    cfg = make_config()
    obs = await refresh(cfg, db, groq_status=503)
    state = build_router_state(cfg, db, cooldowns(), obs, now=NOW, environ=secret_env())
    assert state.health["groq"] == HealthStatus.DOWN


async def test_discovery_failure_falls_back_to_db(db, make_config):
    cfg = make_config()
    await refresh(cfg, db)
    obs = await refresh(cfg, db, gemini_status=500)
    assert obs["gemini"].models_ok is False and obs["gemini"].models == ()
    state = build_router_state(cfg, db, cooldowns(), obs, now=NOW, environ=secret_env())
    assert ("gemini", "g-3.8") in state.capabilities
    assert state.capabilities[("gemini", "g-3.8")].context_window == 100000
    assert state.health["gemini"] == HealthStatus.DOWN


async def test_vanished_models_dropped(db, make_config):
    cfg = make_config()
    await refresh(cfg, db)
    repo = ProviderModelRepository(db)
    assert len(repo.allowed_capabilities(cfg, "gemini")) == 2
    from app.providers.base import ModelCapability

    repo.replace_discovered(cfg, "gemini", [ModelCapability("gemini", "g-3.8")], NOW + H)
    assert [c.model for c in repo.allowed_capabilities(cfg, "gemini")] == ["g-3.8"]
    assert (
        db.execute("SELECT COUNT(*) FROM provider_models WHERE provider='gemini'").fetchone()[0]
        == 1
    )


async def test_estimated_quota_blocks_gemini(db, make_config):
    cfg = make_config(
        providers={
            "gemini": {
                "enabled": True,
                "api_key_env": "GEMINI_API_KEY",
                "limits": [{"dimension": "requests_per_day", "limit": 2}],
            }
        }
    )
    obs = await refresh(cfg, db)
    usage = UsageRepository(db)
    for i in range(2):
        usage.record(
            UsageEvent("gemini", "g-3.8", CostClass.FREE, UsageStatus.SUCCESS, NOW - i * H)
        )
    state = build_router_state(cfg, db, cooldowns(), obs, now=NOW, environ=secret_env())
    est = [r for r in state.quota if r.provider == "gemini"]
    assert len(est) == 1
    assert est[0].confidence == QuotaConfidence.ESTIMATED and est[0].remaining == 0
    d = route(REQ, cfg, state)
    assert isinstance(d, Selected)
    assert d.candidate.provider != "gemini"
    gem = [s for s in d.skipped if s.candidate.provider == "gemini"]
    assert gem and all(s.reason == SkipReason.QUOTA_EXHAUSTED for s in gem)


def _raw_paid(db, cost, at=NOW):
    db.execute(
        "INSERT INTO usage_events (provider, model, cost_class, status, estimated_cost_usd,"
        " occurred_at) VALUES ('openrouter', 'or-paid-1', 'PAID', 'success', ?, ?)",
        (cost, at.isoformat(timespec="microseconds")),
    )


def test_bad_spend_negative(db, make_config):
    cfg = make_config()
    _raw_paid(db, -1.0)
    with pytest.raises(RouterStateError, match="paid_spend"):
        build_router_state(cfg, db, cooldowns(), {}, now=NOW, environ=secret_env())


def test_bad_spend_null_cost(db, make_config):
    cfg = make_config()
    _raw_paid(db, None)
    with pytest.raises(RouterStateError, match="unknown cost"):
        build_router_state(cfg, db, cooldowns(), {}, now=NOW, environ=secret_env())


def test_route_with_state_fails_closed(db, make_config, caplog):
    cfg = make_config()
    _raw_paid(db, -1.0)
    with caplog.at_level(logging.ERROR):
        d = route_with_state(
            REQ,
            cfg,
            lambda: build_router_state(cfg, db, cooldowns(), {}, now=NOW, environ=secret_env()),
        )
    assert d == NoEligibleProvider(NoEligibleReason.ROUTER_STATE_UNAVAILABLE, ())
    assert "router state unavailable" in caplog.text


async def test_route_with_state_attempts_delegates(db, make_config):
    cfg = make_config()
    obs = await refresh(cfg, db)
    state = build_router_state(cfg, db, cooldowns(), obs, now=NOW, environ=secret_env())
    attempts = [FailedAttempt("gemini", "g-3.8", ErrorClass.RATE_LIMITED)]
    expected = next_after_failure(REQ, attempts, cfg, state)
    assert route_with_state(REQ, cfg, lambda: state, attempts=attempts) == expected
    assert route_with_state(REQ, cfg, lambda: state) == route(REQ, cfg, state)
    # non-RouterStateError exceptions propagate
    with pytest.raises(ZeroDivisionError):
        route_with_state(REQ, cfg, lambda: 1 / 0)


def test_paid_spend_day_vs_month(db, make_config):
    cfg = make_config()
    yesterday = NOW - timedelta(days=1)  # 2026-09-29, same UTC month
    _raw_paid(db, 0.5, NOW)
    _raw_paid(db, 0.25, yesterday)
    state = build_router_state(cfg, db, cooldowns(), {}, now=NOW, environ=secret_env())
    assert state.paid_spend_today_usd == 0.5
    assert state.paid_spend_month_usd == 0.75


def test_secret_not_in_state_or_db(db, make_config):
    cfg = make_config()
    state = build_router_state(cfg, db, cooldowns(), {}, now=NOW, environ=secret_env())
    assert SECRET not in repr(state)
