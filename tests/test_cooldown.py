from __future__ import annotations

from datetime import timedelta

from conftest import NOW, full_state

from app.config.models import CooldownConfig
from app.providers.base import ErrorClass, QuotaConfidence, QuotaRecord, QuotaUnit
from app.quota.cooldown import CooldownManager
from app.router.router import route
from app.router.types import RouteRequest, Selected

E = ErrorClass


def mgr(**kw):
    return CooldownManager(CooldownConfig(**kw), clock=lambda: NOW)


def sec(n):
    return NOW + timedelta(seconds=n)


def test_rate_limit_is_model_scoped():
    m = mgr()
    assert m.record_failure("gemini", "g-3.8", E.RATE_LIMITED) == sec(60)
    assert m.is_cooling("gemini", "g-3.8")
    assert not m.is_cooling("gemini", "g-3.7")
    assert m.active() == {("gemini", "g-3.8"): sec(60)}


def test_retry_after_variants():
    m = mgr()
    assert m.record_failure("p", "a", E.RATE_LIMITED, retry_after_s=10) == sec(10)
    assert m.record_failure("p", "b", E.RATE_LIMITED, retry_after_s=99999) == sec(3600)
    assert m.record_failure("p", "c", E.RATE_LIMITED, retry_after_s=0) is None
    assert ("p", "c") not in m.active()
    assert m.record_failure("p", "d", E.QUOTA_EXHAUSTED, retry_after_s=float("nan")) == sec(60)
    assert m.record_failure("p", "e", E.RATE_LIMITED, retry_after_s=float("inf")) == sec(60)


def test_provider_unavailable_is_provider_wide():
    m = mgr()
    assert m.record_failure("x", "m", E.PROVIDER_UNAVAILABLE) == sec(120)
    assert ("x", None) in m.active()
    assert m.is_cooling("x", "anything")


def test_network_failures_threshold():
    m = mgr()
    assert m.record_failure("x", None, E.TRANSIENT_NETWORK, now=NOW) is None
    assert m.record_failure("x", None, E.TIMEOUT_UNKNOWN_OUTCOME, now=sec(1)) is None
    assert m.active(sec(2)) == {}
    assert m.record_failure("x", None, E.TRANSIENT_NETWORK, now=sec(2)) == sec(2 + 120)
    assert ("x", None) in m.active(sec(3))
    # deque cleared: next failure starts over
    assert m.record_failure("y", None, E.TRANSIENT_NETWORK, now=sec(3)) is None


def test_network_failures_outside_window_never_trigger():
    m = mgr()
    for i in range(10):
        assert m.record_failure("x", None, E.TRANSIENT_NETWORK, now=sec(i * 400)) is None
    assert m.active(sec(4000)) == {}


def test_record_success_resets_count():
    m = mgr()
    m.record_failure("x", None, E.TRANSIENT_NETWORK, now=NOW)
    m.record_failure("x", None, E.TRANSIENT_NETWORK, now=sec(1))
    m.record_success("x")
    assert m.record_failure("x", None, E.TRANSIENT_NETWORK, now=sec(2)) is None
    assert m.active(sec(3)) == {}


def test_other_classes_no_cooldown():
    m = mgr()
    for ec in (E.AUTH_FAILED, E.INVALID_REQUEST, E.UNKNOWN, E.POLICY_REJECTED):
        assert m.record_failure("x", "m", ec) is None
    assert m.active() == {}


def test_shorter_does_not_shorten():
    m = mgr()
    m.record_failure("x", "m", E.RATE_LIMITED, retry_after_s=100)
    assert m.record_failure("x", "m", E.RATE_LIMITED, retry_after_s=5) == sec(100)
    m.set_cooldown("x", "m", sec(10))
    assert m.active()[("x", "m")] == sec(100)
    m.set_cooldown("x", "m", sec(200))
    assert m.active()[("x", "m")] == sec(200)


def test_expiry_boundary_and_prune():
    m = mgr()
    until = m.record_failure("x", "m", E.RATE_LIMITED, retry_after_s=10)
    assert m.active(now=until - timedelta(microseconds=1))
    assert m.active(now=until) == {}
    assert m.active(now=NOW) == {}  # pruned for good
    assert not m.is_cooling("x", "m", now=until)


def test_router_integration(make_config):
    cfg = make_config()
    m = mgr()
    m.record_failure("gemini", "g-3.8", E.RATE_LIMITED)
    exhausted = QuotaRecord(
        "openrouter", None, "day", QuotaUnit.REQUESTS, QuotaConfidence.EXHAUSTED, NOW, "t"
    )
    req = RouteRequest("t", "p")
    state = full_state(cfg, quota=[exhausted], cooldowns=m.active(NOW))
    d = route(req, cfg, state)
    assert isinstance(d, Selected)
    assert (d.candidate.provider, d.candidate.model) == ("gemini", "g-3.7")
    later = sec(61)
    state = full_state(cfg, now=later, quota=[exhausted], cooldowns=m.active(later))
    d = route(req, cfg, state)
    assert isinstance(d, Selected)
    assert (d.candidate.provider, d.candidate.model) == ("gemini", "g-3.8")
