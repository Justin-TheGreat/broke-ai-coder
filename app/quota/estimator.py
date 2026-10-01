from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta

from app.config.models import AppConfig
from app.db.usage import UsageRepository
from app.providers.base import QuotaConfidence, QuotaRecord, QuotaUnit
from app.quota.normalize import DIMENSION_SPECS, WINDOW_SECONDS

ESTIMATE_SOURCE = "estimate:config-limit-minus-usage"


def _has_live_exact(
    exact: Sequence[QuotaRecord],
    provider: str,
    model: str | None,
    window: str,
    unit: QuotaUnit,
    now: datetime,
) -> bool:
    return any(
        r.confidence == QuotaConfidence.EXACT
        and r.provider == provider
        and r.model == model
        and r.window == window
        and r.unit == unit
        and (r.reset_at is None or r.reset_at > now)
        for r in exact
    )


def estimate_quota(
    config: AppConfig,
    usage: UsageRepository,
    now: datetime,
    *,
    exact: Sequence[QuotaRecord] = (),
) -> list[QuotaRecord]:
    out: list[QuotaRecord] = []
    for provider, prov in config.providers.items():
        if not prov.enabled:
            continue
        for lim in prov.limits:
            window, unit = DIMENSION_SPECS[lim.dimension]
            if _has_live_exact(exact, provider, lim.model, window, unit, now):
                continue
            window_len = timedelta(seconds=WINDOW_SECONDS[window])
            t = usage.totals(provider, model=lim.model, since=now - window_len, until=now)
            if unit == QuotaUnit.TOKENS:
                if t.events_missing_tokens > 0:
                    out.append(
                        QuotaRecord(
                            provider,
                            lim.model,
                            window,
                            unit,
                            QuotaConfidence.UNKNOWN,
                            now,
                            ESTIMATE_SOURCE,
                            limit=lim.limit,
                            used=None,
                            remaining=None,
                        )
                    )
                    continue
                used: int | float = t.input_tokens + t.output_tokens
            else:
                used = t.requests
            out.append(
                QuotaRecord(
                    provider,
                    lim.model,
                    window,
                    unit,
                    QuotaConfidence.ESTIMATED,
                    now,
                    ESTIMATE_SOURCE,
                    limit=lim.limit,
                    used=used,
                    remaining=max(lim.limit - used, 0),
                    reset_at=None if t.earliest is None else t.earliest + window_len,
                )
            )
    return out
