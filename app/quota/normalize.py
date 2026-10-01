from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from datetime import datetime

from app.providers.base import QuotaConfidence, QuotaDimension, QuotaRecord, QuotaUnit

WINDOW_SECONDS: Mapping[str, int] = {"minute": 60, "hour": 3600, "day": 86400}

DIMENSION_SPECS: Mapping[QuotaDimension, tuple[str, QuotaUnit]] = {
    QuotaDimension.REQUESTS_PER_MINUTE: ("minute", QuotaUnit.REQUESTS),
    QuotaDimension.REQUESTS_PER_HOUR: ("hour", QuotaUnit.REQUESTS),
    QuotaDimension.REQUESTS_PER_DAY: ("day", QuotaUnit.REQUESTS),
    QuotaDimension.TOKENS_PER_MINUTE: ("minute", QuotaUnit.TOKENS),
    QuotaDimension.TOKENS_PER_HOUR: ("hour", QuotaUnit.TOKENS),
    QuotaDimension.TOKENS_PER_DAY: ("day", QuotaUnit.TOKENS),
    QuotaDimension.SPEND_USD: ("total", QuotaUnit.USD),
}
_SPEC_TO_DIMENSION: Mapping[tuple[str, QuotaUnit], QuotaDimension] = {
    spec: dim for dim, spec in DIMENSION_SPECS.items()
}

ESTIMATE_SOURCE_PREFIX = "estimate:"
CONFIG_SOURCE_PREFIX = "config:"


def dimension_of(record: QuotaRecord) -> QuotaDimension | None:
    return _SPEC_TO_DIMENSION.get((record.window, record.unit))


def _check_number(name: str, value: object) -> None:
    if value is None:
        return
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValueError(f"{name} must be a number")
    if not math.isfinite(value) or value < 0:
        raise ValueError(f"{name} must be finite and >= 0")


def _check_aware(name: str, value: datetime | None) -> None:
    if value is None:
        return
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")


def validate_record(record: QuotaRecord) -> None:
    _check_number("limit", record.limit)
    _check_number("used", record.used)
    _check_number("remaining", record.remaining)
    if not record.source:
        raise ValueError("source must be non-empty")
    _check_aware("observed_at", record.observed_at)
    _check_aware("reset_at", record.reset_at)
    if (
        record.confidence in (QuotaConfidence.EXACT, QuotaConfidence.ESTIMATED)
        and record.remaining is None
    ):
        raise ValueError(f"{record.confidence} record requires remaining")
    if record.confidence == QuotaConfidence.EXACT and record.source.startswith(
        (ESTIMATE_SOURCE_PREFIX, CONFIG_SOURCE_PREFIX)
    ):
        raise ValueError("estimate/config-derived record cannot be EXACT")


def normalize(
    records: Iterable[QuotaRecord],
) -> dict[tuple[str, str | None], dict[QuotaDimension, QuotaRecord | None]]:
    out: dict[tuple[str, str | None], dict[QuotaDimension, QuotaRecord | None]] = {}
    for rec in records:
        dim = dimension_of(rec)
        if dim is None:
            continue
        slot = out.setdefault((rec.provider, rec.model), {d: None for d in QuotaDimension})
        current = slot[dim]
        if current is None or rec.observed_at >= current.observed_at:
            slot[dim] = rec
    return out
