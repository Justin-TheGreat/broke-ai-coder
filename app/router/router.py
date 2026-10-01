from __future__ import annotations

from collections.abc import Sequence

from app.config.models import AppConfig
from app.providers.base import (
    PROVIDER_FALLBACK_ERRORS,
    STOP_ERRORS,
    HealthStatus,
    ModelCapability,
    QuotaConfidence,
    QuotaUnit,
)
from app.router.types import (
    Candidate,
    FailedAttempt,
    NoEligibleProvider,
    NoEligibleReason,
    RouteDecision,
    RouteRequest,
    RouterState,
    Selected,
    Skipped,
    SkipReason,
)


def build_candidates(config: AppConfig) -> list[Candidate]:
    routing = config.routing
    out: list[Candidate] = []
    for provider_rank, policy_id in enumerate(routing.provider_order):
        policy = routing.policies[policy_id]
        for model_rank, model in enumerate(policy.model_order):
            out.append(
                Candidate(
                    policy_id=policy_id,
                    provider=policy.provider,
                    model=model,
                    provider_rank=provider_rank,
                    model_rank=model_rank,
                )
            )
    return out


def _capability_mismatch(
    request: RouteRequest, cap: ModelCapability, large_context_min: int
) -> bool:
    req = request.required_capabilities
    if req.tool_calling and not cap.supports_tool_calling:
        return True
    if req.structured_output and not cap.supports_structured_output:
        return True
    if req.vision and not cap.supports_vision:
        return True
    if req.large_context and not (
        cap.context_window is not None and cap.context_window >= large_context_min
    ):
        return True
    return False


def _skip_reason(
    cand: Candidate,
    request: RouteRequest,
    config: AppConfig,
    state: RouterState,
    excluded_pairs: frozenset[tuple[str, str]],
    excluded_providers: frozenset[str],
) -> SkipReason | None:
    routing = config.routing
    provider, model = cand.provider, cand.model
    now = state.now

    if (provider, model) in excluded_pairs or provider in excluded_providers:
        return SkipReason.EXCLUDED_AFTER_FAILURE
    if not routing.policies[cand.policy_id].enabled:
        return SkipReason.POLICY_DISABLED
    if not config.providers[provider].enabled:
        return SkipReason.PROVIDER_DISABLED
    if provider not in state.credentials_present:
        return SkipReason.MISSING_CREDENTIAL
    if state.health.get(provider, HealthStatus.HEALTHY) == HealthStatus.DOWN:
        return SkipReason.PROVIDER_DOWN
    for key in ((provider, None), (provider, model)):
        until = state.cooldowns.get(key)
        if until is not None and until > now:
            return SkipReason.COOLDOWN
    cap = state.capabilities.get((provider, model))
    if cap is None:
        return SkipReason.MODEL_UNKNOWN
    if _capability_mismatch(request, cap, routing.large_context_min_tokens):
        return SkipReason.CAPABILITY_MISMATCH
    est_in = request.estimated_input_tokens
    est_out = request.estimated_output_tokens
    if cap.context_window is not None and est_in + est_out > cap.context_window:
        return SkipReason.CONTEXT_TOO_SMALL
    if cap.max_output_tokens is not None and est_out > cap.max_output_tokens:
        return SkipReason.CONTEXT_TOO_SMALL

    for r in state.quota:
        if r.provider != provider or r.model not in (None, model):
            continue
        if r.reset_at is not None and r.reset_at <= now:
            continue
        if r.confidence == QuotaConfidence.EXHAUSTED:
            return SkipReason.QUOTA_EXHAUSTED
        if r.confidence == QuotaConfidence.COOLDOWN:
            return SkipReason.COOLDOWN
        if r.confidence in (QuotaConfidence.EXACT, QuotaConfidence.ESTIMATED) and (
            r.remaining is not None
        ):
            if r.unit == QuotaUnit.REQUESTS and r.remaining < 1:
                return SkipReason.QUOTA_EXHAUSTED
            if r.unit == QuotaUnit.TOKENS and r.remaining < est_in + est_out:
                return SkipReason.QUOTA_INSUFFICIENT
    return None


def route(
    request: RouteRequest,
    config: AppConfig,
    state: RouterState,
    *,
    excluded_pairs: frozenset[tuple[str, str]] = frozenset(),
    excluded_providers: frozenset[str] = frozenset(),
) -> RouteDecision:
    # Every candidate is free (paid inference is unrepresentable), so the first
    # eligible one in configured order wins; when none is eligible the task stops.
    # All candidates are evaluated so `skipped` explains every ineligible one.
    skipped: list[Skipped] = []
    eligible: list[Candidate] = []
    for cand in build_candidates(config):
        reason = _skip_reason(cand, request, config, state, excluded_pairs, excluded_providers)
        if reason is None:
            eligible.append(cand)
        else:
            skipped.append(Skipped(cand, reason))
    if eligible:
        return Selected(eligible[0], tuple(skipped))
    return NoEligibleProvider(NoEligibleReason.FREE_CAPACITY_EXHAUSTED, tuple(skipped))


def next_after_failure(
    request: RouteRequest,
    attempts: Sequence[FailedAttempt],
    config: AppConfig,
    state: RouterState,
) -> RouteDecision:
    if not attempts:
        raise ValueError("attempts must not be empty")
    if attempts[-1].error_class in STOP_ERRORS:
        return NoEligibleProvider(NoEligibleReason.NON_FALLBACK_ERROR, ())
    if len(attempts) >= config.routing.max_fallback_attempts:
        return NoEligibleProvider(NoEligibleReason.MAX_FALLBACK_ATTEMPTS_REACHED, ())
    excluded_pairs = frozenset((a.provider, a.model) for a in attempts)
    excluded_providers = frozenset(
        a.provider for a in attempts if a.error_class in PROVIDER_FALLBACK_ERRORS
    )
    return route(
        request,
        config,
        state,
        excluded_pairs=excluded_pairs,
        excluded_providers=excluded_providers,
    )
