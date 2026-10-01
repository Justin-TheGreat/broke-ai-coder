from __future__ import annotations

from datetime import timedelta

from conftest import NOW

from app.db.usage import UsageEvent, UsageRepository, UsageStatus
from app.providers.base import CostClass, QuotaConfidence, QuotaRecord, QuotaUnit
from app.quota.estimator import ESTIMATE_SOURCE, estimate_quota
from app.quota.normalize import validate_record

H = timedelta(hours=1)


def cfg_with(make_config, limits, **prov):
    base = {"enabled": True, "api_key_env": "GEMINI_API_KEY", "limits": limits}
    base.update(prov)
    return make_config(providers={"gemini": base})


def add(repo, n=1, *, at=NOW, model="g-3.8", **kw):
    for _ in range(n):
        repo.record(
            UsageEvent(
                "gemini",
                model,
                CostClass.FREE,
                UsageStatus.SUCCESS,
                at,
                input_tokens=kw.get("i", 10),
                output_tokens=kw.get("o", 5),
            )
        )


RPD = {"dimension": "requests_per_day", "limit": 5}


def test_rpd_estimate(db, make_config):
    repo = UsageRepository(db)
    add(repo, 3, at=NOW - H)
    add(repo, 1, at=NOW - 25 * H)
    cfg = cfg_with(make_config, [RPD])
    (r,) = estimate_quota(cfg, repo, NOW)
    assert r.confidence == QuotaConfidence.ESTIMATED
    assert (r.used, r.remaining, r.limit) == (3, 2, 5)
    assert r.window == "day" and r.unit == QuotaUnit.REQUESTS and r.model is None
    assert r.source == ESTIMATE_SOURCE
    assert r.reset_at == NOW - H + 24 * H
    validate_record(r)


def test_over_limit_remaining_zero(db, make_config):
    repo = UsageRepository(db)
    add(repo, 7)
    (r,) = estimate_quota(cfg_with(make_config, [RPD]), repo, NOW)
    assert r.remaining == 0 and r.used == 7


def test_model_scoped(db, make_config):
    repo = UsageRepository(db)
    add(repo, 2, model="g-3.8")
    add(repo, 4, model="g-3.7")
    lim = {"dimension": "requests_per_day", "limit": 10, "model": "g-3.8"}
    (r,) = estimate_quota(cfg_with(make_config, [lim]), repo, NOW)
    assert (r.model, r.used) == ("g-3.8", 2)


def test_tokens_estimated_and_missing(db, make_config):
    repo = UsageRepository(db)
    add(repo, 2)
    lim = {"dimension": "tokens_per_minute", "limit": 100}
    (r,) = estimate_quota(cfg_with(make_config, [lim]), repo, NOW)
    assert (r.used, r.remaining, r.window, r.unit) == (30, 70, "minute", QuotaUnit.TOKENS)
    repo.record(UsageEvent("gemini", "g-3.8", CostClass.FREE, UsageStatus.ERROR, NOW))
    (r,) = estimate_quota(cfg_with(make_config, [lim]), repo, NOW)
    assert r.confidence == QuotaConfidence.UNKNOWN
    assert r.remaining is None and r.used is None and r.limit == 100


def exact(**kw):
    base = dict(
        provider="gemini",
        model=None,
        window="day",
        unit=QuotaUnit.REQUESTS,
        confidence=QuotaConfidence.EXACT,
        observed_at=NOW,
        source="gemini:x",
        limit=5,
        used=1,
        remaining=4,
        reset_at=NOW + H,
    )
    base.update(kw)
    return QuotaRecord(**base)


def test_exact_suppresses_unexpired_only(db, make_config):
    repo = UsageRepository(db)
    cfg = cfg_with(make_config, [RPD])
    assert estimate_quota(cfg, repo, NOW, exact=[exact()]) == []
    assert estimate_quota(cfg, repo, NOW, exact=[exact(reset_at=None)]) == []
    assert len(estimate_quota(cfg, repo, NOW, exact=[exact(reset_at=NOW)])) == 1
    assert len(estimate_quota(cfg, repo, NOW, exact=[exact(model="g-3.8")])) == 1


def test_disabled_provider_no_records(db, make_config):
    repo = UsageRepository(db)
    cfg = cfg_with(make_config, [RPD], enabled=False)
    assert estimate_quota(cfg, repo, NOW) == []


def test_never_exact(db, make_config):
    repo = UsageRepository(db)
    add(repo, 2)
    lims = [
        RPD,
        {"dimension": "tokens_per_hour", "limit": 50},
        {"dimension": "requests_per_minute", "limit": 3, "model": "g-3.8"},
    ]
    out = estimate_quota(cfg_with(make_config, lims), repo, NOW)
    assert len(out) == 3
    for r in out:
        assert r.confidence != QuotaConfidence.EXACT
        validate_record(r)
