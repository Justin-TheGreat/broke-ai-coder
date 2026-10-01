from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

import pytest
from conftest import NOW, full_state

from app.providers.base import (
    HealthStatus,
    ModelCapability,
    QuotaConfidence,
    QuotaRecord,
    QuotaUnit,
)
from app.router import (
    NoEligibleProvider,
    NoEligibleReason,
    PaidApprovalRequired,
    RequiredCapabilities,
    RouteRequest,
    Selected,
    SkipReason,
    build_candidates,
    route,
)

REQ = RouteRequest("t", "p")
PAID_MODE = "free-first-paid-after-approval"


def q(provider, confidence, model=None, unit=QuotaUnit.REQUESTS, **kw):
    return QuotaRecord(provider, model, "day", unit, confidence, NOW, "t", **kw)


def exhaust(*providers):
    return tuple(q(p, QuotaConfidence.EXHAUSTED) for p in providers)


def cd(*pairs, until=None):
    until = until or NOW + timedelta(minutes=5)
    return {p: until for p in pairs}


def model_of(d):
    assert isinstance(d, Selected), d
    return d.candidate.model


def reasons(d):
    return {s.candidate.model: s.reason for s in d.skipped}


def test_selects_first_free(make_config):
    cfg = make_config()
    assert model_of(route(REQ, cfg, full_state(cfg))) == "openrouter/free"


def test_openrouter_exhausted_goes_to_gemini_38(make_config):
    cfg = make_config()
    d = route(REQ, cfg, full_state(cfg, quota=exhaust("openrouter")))
    assert model_of(d) == "g-3.8"
    assert reasons(d)["openrouter/free"] == SkipReason.QUOTA_EXHAUSTED


def test_g38_cooldown_goes_to_g37_never_unlisted(make_config):
    cfg = make_config()
    st = full_state(cfg, quota=exhaust("openrouter"), cooldowns=cd(("gemini", "g-3.8")))
    caps = dict(st.capabilities)
    caps[("gemini", "g-pro")] = ModelCapability("gemini", "g-pro", supports_tool_calling=True)
    st = replace(st, capabilities=caps)
    assert model_of(route(REQ, cfg, st)) == "g-3.7"


def test_both_gemini_out_goes_to_cerebras(make_config):
    cfg = make_config()
    st = full_state(
        cfg,
        quota=exhaust("openrouter"),
        cooldowns=cd(("gemini", "g-3.8"), ("gemini", "g-3.7")),
    )
    assert model_of(route(REQ, cfg, st)) == "c-a"


def test_swapping_config_order_changes_selection(make_config):
    cfg = make_config()
    data = cfg.model_dump(mode="json")
    data["routing"]["policies"]["gemini-free"]["model_order"] = ["g-3.7", "g-3.8"]
    from app.config.loader import parse_config

    swapped = parse_config(data)
    assert model_of(route(REQ, cfg, full_state(cfg, quota=exhaust("openrouter")))) == "g-3.8"
    d = route(REQ, swapped, full_state(swapped, quota=exhaust("openrouter")))
    assert model_of(d) == "g-3.7"


def test_removed_model_never_selected(make_config):
    cfg = make_config()
    data = cfg.model_dump(mode="json")
    data["routing"]["policies"]["gemini-free"]["model_order"] = ["g-3.7"]
    from app.config.loader import parse_config

    cfg2 = parse_config(data)
    st = full_state(cfg, quota=exhaust("openrouter"))  # capabilities still include g-3.8
    d = route(REQ, cfg2, st)
    assert model_of(d) == "g-3.7"
    st2 = full_state(cfg, quota=exhaust("openrouter"), cooldowns=cd(("gemini", "g-3.7")))
    assert model_of(route(REQ, cfg2, st2)) == "c-a"
    assert all(c.model != "g-3.8" for c in build_candidates(cfg2))


def test_capability_mismatch_vision(make_config):
    cfg = make_config()
    st = full_state(cfg)
    caps = dict(st.capabilities)
    caps[("groq", "q-y")] = replace(caps[("groq", "q-y")], supports_vision=True)
    st = replace(st, capabilities=caps)
    req = RouteRequest("t", "p", required_capabilities=RequiredCapabilities(vision=True))
    d = route(req, cfg, st)
    assert model_of(d) == "q-y"
    assert reasons(d)["openrouter/free"] == SkipReason.CAPABILITY_MISMATCH


def test_large_context_enforced(make_config):
    cfg = make_config(routing={"large_context_min_tokens": 500_000})
    req = RouteRequest("t", "p", required_capabilities=RequiredCapabilities(large_context=True))
    d = route(req, cfg, full_state(cfg))
    assert isinstance(d, NoEligibleProvider)
    assert d.reason == NoEligibleReason.FREE_CAPACITY_EXHAUSTED


def test_tokens_above_context_window(make_config):
    cfg = make_config()
    req = RouteRequest("t", "p", estimated_input_tokens=190_000, estimated_output_tokens=20_000)
    d = route(req, cfg, full_state(cfg))
    assert isinstance(d, NoEligibleProvider)
    assert all(
        s.reason == SkipReason.CONTEXT_TOO_SMALL
        for s in d.skipped
        if s.candidate.cost_class == "FREE"
    )


def test_output_above_max_output(make_config):
    cfg = make_config()
    req = RouteRequest("t", "p", estimated_output_tokens=33_000)
    d = route(req, cfg, full_state(cfg))
    assert isinstance(d, NoEligibleProvider)


def test_missing_capability_metadata(make_config):
    cfg = make_config()
    st = full_state(cfg)
    caps = {k: v for k, v in st.capabilities.items() if k != ("openrouter", "openrouter/free")}
    d = route(REQ, cfg, replace(st, capabilities=caps))
    assert reasons(d)["openrouter/free"] == SkipReason.MODEL_UNKNOWN
    assert model_of(d) == "g-3.8"


def test_missing_credential(make_config):
    cfg = make_config()
    st = replace(full_state(cfg), credentials_present=frozenset({"gemini"}))
    d = route(REQ, cfg, st)
    assert model_of(d) == "g-3.8"
    assert reasons(d)["openrouter/free"] == SkipReason.MISSING_CREDENTIAL


def test_disabled_provider_and_policy(make_config):
    cfg = make_config()
    data = cfg.model_dump(mode="json")
    data["providers"]["openrouter"]["enabled"] = False
    data["routing"]["policies"]["gemini-free"]["enabled"] = False
    from app.config.loader import parse_config

    c2 = parse_config(data)
    d = route(REQ, c2, full_state(c2))
    assert model_of(d) == "c-a"
    r = reasons(d)
    assert r["openrouter/free"] == SkipReason.PROVIDER_DISABLED
    assert r["g-3.8"] == SkipReason.POLICY_DISABLED


def test_health_down_skipped_degraded_allowed(make_config):
    cfg = make_config()
    d = route(REQ, cfg, full_state(cfg, health={"openrouter": HealthStatus.DOWN}))
    assert model_of(d) == "g-3.8"
    assert reasons(d)["openrouter/free"] == SkipReason.PROVIDER_DOWN
    d = route(REQ, cfg, full_state(cfg, health={"openrouter": HealthStatus.DEGRADED}))
    assert model_of(d) == "openrouter/free"


def test_provider_wide_cooldown(make_config):
    cfg = make_config()
    st = full_state(cfg, cooldowns=cd(("gemini", None)), quota=exhaust("openrouter"))
    assert model_of(route(REQ, cfg, st)) == "c-a"


def test_expired_and_boundary_cooldown_and_quota(make_config):
    cfg = make_config()
    # until == now is expired
    st = full_state(cfg, cooldowns={("openrouter", None): NOW})
    assert model_of(route(REQ, cfg, st)) == "openrouter/free"
    # just in the future is active
    st = full_state(cfg, cooldowns={("openrouter", None): NOW + timedelta(microseconds=1)})
    assert model_of(route(REQ, cfg, st)) == "g-3.8"
    # expired exhausted quota (reset_at == now) does not filter
    st = full_state(cfg, quota=(q("openrouter", QuotaConfidence.EXHAUSTED, reset_at=NOW),))
    assert model_of(route(REQ, cfg, st)) == "openrouter/free"
    st = full_state(
        cfg,
        quota=(q("openrouter", QuotaConfidence.EXHAUSTED, reset_at=NOW + timedelta(seconds=1)),),
    )
    assert model_of(route(REQ, cfg, st)) == "g-3.8"


def test_requests_remaining_zero_skipped(make_config):
    cfg = make_config()
    rec = q("openrouter", QuotaConfidence.EXACT, remaining=0)
    d = route(REQ, cfg, full_state(cfg, quota=(rec,)))
    assert reasons(d)["openrouter/free"] == SkipReason.QUOTA_EXHAUSTED
    assert model_of(d) == "g-3.8"


def test_tokens_remaining_insufficient(make_config):
    cfg = make_config()
    rec = q("openrouter", QuotaConfidence.ESTIMATED, unit=QuotaUnit.TOKENS, remaining=100)
    req = RouteRequest("t", "p", estimated_input_tokens=80, estimated_output_tokens=30)
    d = route(req, cfg, full_state(cfg, quota=(rec,)))
    assert reasons(d)["openrouter/free"] == SkipReason.QUOTA_INSUFFICIENT
    req = RouteRequest("t", "p", estimated_input_tokens=70, estimated_output_tokens=30)
    assert model_of(route(req, cfg, full_state(cfg, quota=(rec,)))) == "openrouter/free"


def test_unknown_quota_does_not_filter(make_config):
    cfg = make_config()
    rec = q("openrouter", QuotaConfidence.UNKNOWN, remaining=None)
    assert model_of(route(REQ, cfg, full_state(cfg, quota=(rec,)))) == "openrouter/free"


ALL_FREE = ("openrouter", "gemini", "cerebras", "groq")


def test_default_mode_never_paid_even_if_only_candidate(make_config):
    cfg = make_config(routing={"daily_paid_budget_usd": 100.0})
    d = route(
        REQ,
        cfg,
        full_state(
            cfg,
            quota=exhaust("gemini", "cerebras", "groq"),
            cooldowns=cd(("openrouter", "openrouter/free")),
        ),
    )
    assert isinstance(d, NoEligibleProvider)
    assert d.reason == NoEligibleReason.FREE_CAPACITY_EXHAUSTED
    assert reasons(d)["or-paid-1"] == SkipReason.PAID_BLOCKED_BY_MODE


@pytest.mark.parametrize("mode", ["free-only", "free-first-no-paid"])
def test_paid_blocked_modes(make_config, mode):
    cfg = make_config(
        routing={"mode": mode, "daily_paid_budget_usd": 100.0, "paid_requires_approval": False}
    )
    d = route(
        REQ,
        cfg,
        full_state(
            cfg,
            quota=exhaust("gemini", "cerebras", "groq"),
            cooldowns=cd(("openrouter", "openrouter/free")),
        ),
    )
    assert isinstance(d, NoEligibleProvider)
    assert d.reason == NoEligibleReason.FREE_CAPACITY_EXHAUSTED
    assert reasons(d)["or-paid-1"] == SkipReason.PAID_BLOCKED_BY_MODE
    # approved flag makes no difference
    req = RouteRequest("t", "p", paid_approved=True)
    d2 = route(
        req,
        cfg,
        full_state(
            cfg,
            quota=exhaust("gemini", "cerebras", "groq"),
            cooldowns=cd(("openrouter", "openrouter/free")),
        ),
    )
    assert isinstance(d2, NoEligibleProvider)


def free_down(cfg, **kw):
    return full_state(
        cfg,
        quota=exhaust("gemini", "cerebras", "groq"),
        cooldowns=cd(("openrouter", "openrouter/free")),
        **kw,
    )


def paid_cfg(**routing):
    return {"mode": PAID_MODE, **routing}


def test_paid_budget_zero(make_config):
    cfg = make_config(routing=paid_cfg(daily_paid_budget_usd=0.0))
    d = route(REQ, cfg, free_down(cfg))
    assert isinstance(d, NoEligibleProvider)
    assert d.reason == NoEligibleReason.PAID_BUDGET_EXHAUSTED
    d = route(RouteRequest("t", "p", paid_approved=True), cfg, free_down(cfg))
    assert isinstance(d, NoEligibleProvider)


def test_paid_requires_approval(make_config):
    cfg = make_config(routing=paid_cfg(daily_paid_budget_usd=5.0))
    d = route(REQ, cfg, free_down(cfg))
    assert isinstance(d, PaidApprovalRequired)
    assert [c.model for c in d.candidates] == ["or-paid-1"]


def test_paid_approved_selected(make_config):
    cfg = make_config(routing=paid_cfg(daily_paid_budget_usd=5.0))
    d = route(RouteRequest("t", "p", paid_approved=True), cfg, free_down(cfg))
    assert model_of(d) == "or-paid-1"


def test_paid_no_approval_required(make_config):
    cfg = make_config(routing=paid_cfg(daily_paid_budget_usd=5.0, paid_requires_approval=False))
    assert model_of(route(REQ, cfg, free_down(cfg))) == "or-paid-1"


def test_paid_budget_spent_and_monthly(make_config):
    cfg = make_config(routing=paid_cfg(daily_paid_budget_usd=5.0))
    d = route(REQ, cfg, free_down(cfg, paid_spend_today_usd=5.0))
    assert isinstance(d, NoEligibleProvider)
    assert d.reason == NoEligibleReason.PAID_BUDGET_EXHAUSTED
    cfg = make_config(routing=paid_cfg(daily_paid_budget_usd=5.0, monthly_paid_budget_usd=10.0))
    d = route(REQ, cfg, free_down(cfg, paid_spend_month_usd=10.0))
    assert isinstance(d, NoEligibleProvider)
    assert d.reason == NoEligibleReason.PAID_BUDGET_EXHAUSTED


def test_free_always_beats_paid(make_config):
    cfg = make_config(routing=paid_cfg(daily_paid_budget_usd=5.0))
    d = route(RouteRequest("t", "p", paid_approved=True), cfg, full_state(cfg))
    assert model_of(d) == "openrouter/free"
    assert d.candidate.cost_class == "FREE"


def test_deterministic(make_config):
    cfg = make_config()
    st = full_state(cfg, quota=exhaust("openrouter"))
    assert route(REQ, cfg, st) == route(REQ, cfg, st)


def test_skipped_in_candidate_order(make_config):
    cfg = make_config()
    st = full_state(cfg, quota=exhaust("openrouter", "gemini"))
    d = route(REQ, cfg, st)
    order = [c.model for c in build_candidates(cfg)]
    got = [s.candidate.model for s in d.skipped]
    assert got == [m for m in order if m in got]
    assert model_of(d) == "c-a"


def test_candidates_sorted_by_config_order(make_config):
    cfg = make_config()
    cands = build_candidates(cfg)
    assert [c.sort_key for c in cands] == sorted(c.sort_key for c in cands)
    assert [c.model for c in cands][:3] == ["openrouter/free", "g-3.8", "g-3.7"]


def test_negative_tokens_rejected():
    with pytest.raises(ValueError):
        RouteRequest("t", "p", estimated_input_tokens=-1)
    with pytest.raises(ValueError):
        RouteRequest("t", "p", estimated_output_tokens=-1)
