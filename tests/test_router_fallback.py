from __future__ import annotations

import pytest
from conftest import full_state

from app.providers.base import ErrorClass
from app.router import (
    FailedAttempt,
    NoEligibleProvider,
    NoEligibleReason,
    RouteRequest,
    Selected,
    SkipReason,
    next_after_failure,
)

REQ = RouteRequest("t", "p")
E = ErrorClass


def fa(provider, model, ec=E.RATE_LIMITED):
    return FailedAttempt(provider, model, ec)


OR = fa("openrouter", "openrouter/free")
G38 = fa("gemini", "g-3.8")
G37 = fa("gemini", "g-3.7", E.QUOTA_EXHAUSTED)


def model_of(d):
    assert isinstance(d, Selected), d
    return d.candidate.model


def test_chain_to_gemini_then_37(make_config):
    cfg = make_config(routing={"max_fallback_attempts": 5})
    st = full_state(cfg)
    assert model_of(next_after_failure(REQ, [OR], cfg, st)) == "g-3.8"
    assert model_of(next_after_failure(REQ, [OR, G38], cfg, st)) == "g-3.7"
    assert model_of(next_after_failure(REQ, [OR, G38, G37], cfg, st)) == "q-x"


def test_max_attempts_default_stops(make_config):
    cfg = make_config()
    d = next_after_failure(REQ, [OR, G38, G37], cfg, full_state(cfg))
    assert isinstance(d, NoEligibleProvider)
    assert d.reason == NoEligibleReason.MAX_FALLBACK_ATTEMPTS_REACHED


def test_context_too_large_goes_to_qy(make_config):
    cfg = make_config(routing={"max_fallback_attempts": 9})
    st = full_state(cfg)
    attempts = [
        fa("openrouter", "openrouter/free", E.QUOTA_EXHAUSTED),
        fa("gemini", "g-3.8"),
        fa("gemini", "g-3.7"),
        fa("groq", "q-x", E.CONTEXT_TOO_LARGE),
    ]
    assert model_of(next_after_failure(REQ, attempts, cfg, st)) == "q-y"


def test_auth_failed_excludes_whole_provider(make_config):
    cfg = make_config(routing={"max_fallback_attempts": 20})
    st = full_state(cfg)
    attempts = [fa("openrouter", "openrouter/free", E.AUTH_FAILED)]
    for m, p in [
        ("g-3.8", "gemini"),
        ("g-3.7", "gemini"),
        ("q-x", "groq"),
        ("q-y", "groq"),
    ]:
        attempts.append(fa(p, m))
    d = next_after_failure(REQ, attempts, cfg, st)
    assert isinstance(d, NoEligibleProvider)
    assert d.reason == NoEligibleReason.FREE_CAPACITY_EXHAUSTED


def test_provider_unavailable_skips_g37(make_config):
    cfg = make_config()
    d = next_after_failure(
        REQ, [OR, fa("gemini", "g-3.8", E.PROVIDER_UNAVAILABLE)], cfg, full_state(cfg)
    )
    assert model_of(d) == "q-x"


@pytest.mark.parametrize(
    "ec", [E.TIMEOUT_UNKNOWN_OUTCOME, E.INVALID_REQUEST, E.UNKNOWN, E.POLICY_REJECTED]
)
def test_stop_errors(make_config, ec):
    cfg = make_config()
    d = next_after_failure(REQ, [fa("openrouter", "openrouter/free", ec)], cfg, full_state(cfg))
    assert isinstance(d, NoEligibleProvider)
    assert d.reason == NoEligibleReason.NON_FALLBACK_ERROR


def test_exhausting_every_free_candidate_stops_the_task(make_config):
    cfg = make_config(routing={"max_fallback_attempts": 20})
    attempts = [
        fa("openrouter", "openrouter/free"),
        fa("gemini", "g-3.8"),
        fa("gemini", "g-3.7"),
        fa("groq", "q-x"),
        fa("groq", "q-y"),
    ]
    d = next_after_failure(REQ, attempts, cfg, full_state(cfg))
    assert isinstance(d, NoEligibleProvider)
    assert d.reason == NoEligibleReason.FREE_CAPACITY_EXHAUSTED
    assert {s.reason for s in d.skipped} == {SkipReason.EXCLUDED_AFTER_FAILURE}


def test_empty_attempts(make_config):
    cfg = make_config()
    with pytest.raises(ValueError):
        next_after_failure(REQ, [], cfg, full_state(cfg))
