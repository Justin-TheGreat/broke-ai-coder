from __future__ import annotations

import math
import re
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime

from app.providers.base import QuotaConfidence, QuotaDimension, QuotaRecord
from app.quota.normalize import DIMENSION_SPECS

_PLAIN_NUMBER = re.compile(r"^\d+(\.\d+)?$")
_DURATION_PART = re.compile(r"(\d+(?:\.\d+)?)(ms|us|µs|h|m|s)")
_UNIT_SECONDS = {"h": 3600.0, "m": 60.0, "s": 1.0, "ms": 0.001, "us": 1e-6, "µs": 1e-6}


def parse_duration(value: str | None) -> float | None:
    if value is None:
        return None
    text = value.strip()
    if not text:
        return None
    if _PLAIN_NUMBER.match(text):
        return float(text)
    pos = 0
    total = 0.0
    while pos < len(text):
        m = _DURATION_PART.match(text, pos)
        if m is None:
            return None
        total += float(m.group(1)) * _UNIT_SECONDS[m.group(2)]
        pos = m.end()
    return total


def parse_number(value: str | None) -> int | float | None:
    if value is None:
        return None
    text = value.strip()
    if not _PLAIN_NUMBER.match(text):
        return None
    if "." not in text:
        return int(text)
    num = float(text)
    if not math.isfinite(num):
        return None
    return int(num) if num.is_integer() else num


def parse_retry_after(value: str | None, now: datetime) -> float | None:
    if value is None:
        return None
    text = value.strip()
    if not text:
        return None
    if _PLAIN_NUMBER.match(text):
        return float(text)
    try:
        dt = parsedate_to_datetime(text)
    except (TypeError, ValueError, IndexError):
        return None
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return max(0.0, (dt - now).total_seconds())


def parse_rate_limit_headers(
    headers: Mapping[str, str],
    dimensions: Mapping[str, QuotaDimension],
    *,
    provider: str,
    model: str | None,
    now: datetime,
) -> list[QuotaRecord]:
    lowered = {k.lower(): v for k, v in headers.items()}
    out: list[QuotaRecord] = []
    for suffix, dim in dimensions.items():
        limit = parse_number(lowered.get(f"x-ratelimit-limit-{suffix}"))
        remaining = parse_number(lowered.get(f"x-ratelimit-remaining-{suffix}"))
        if limit is None and remaining is None:
            continue
        reset_s = parse_duration(lowered.get(f"x-ratelimit-reset-{suffix}"))
        used = limit - remaining if limit is not None and remaining is not None else None
        if used is not None and used < 0:
            used = None
        window, unit = DIMENSION_SPECS[dim]
        out.append(
            QuotaRecord(
                provider=provider,
                model=model,
                window=window,
                unit=unit,
                confidence=(
                    QuotaConfidence.EXACT if remaining is not None else QuotaConfidence.UNKNOWN
                ),
                observed_at=now,
                source=f"{provider}:response-headers",
                limit=limit,
                used=used,
                remaining=remaining,
                reset_at=None if reset_s is None else now + timedelta(seconds=reset_s),
            )
        )
    return out
