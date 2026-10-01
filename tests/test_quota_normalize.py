from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta

import pytest
from conftest import NOW

from app.providers.base import QuotaConfidence, QuotaDimension, QuotaRecord, QuotaUnit
from app.quota.normalize import DIMENSION_SPECS, dimension_of, normalize, validate_record


def rec(**kw):
    base = dict(
        provider="p",
        model=None,
        window="day",
        unit=QuotaUnit.REQUESTS,
        confidence=QuotaConfidence.EXACT,
        observed_at=NOW,
        source="p:hdr",
        limit=10,
        used=3,
        remaining=7,
    )
    base.update(kw)
    return QuotaRecord(**base)


def test_specs_cover_all_dimensions_uniquely():
    assert set(DIMENSION_SPECS) == set(QuotaDimension)
    assert len(set(DIMENSION_SPECS.values())) == 7
    assert dimension_of(rec()) == QuotaDimension.REQUESTS_PER_DAY
    assert dimension_of(rec(window="week")) is None


def test_normalize_missing_dimensions_are_none():
    slot = normalize([rec()])[("p", None)]
    assert set(slot) == set(QuotaDimension)
    assert slot[QuotaDimension.REQUESTS_PER_DAY] is not None
    assert sum(v is None for v in slot.values()) == 6


def test_newest_wins_and_tie_later_wins():
    old = rec(observed_at=NOW - timedelta(hours=1), remaining=9)
    new = rec(remaining=1)
    assert normalize([new, old])[("p", None)][QuotaDimension.REQUESTS_PER_DAY] == new
    a, b = rec(remaining=1), rec(remaining=2)
    assert normalize([a, b])[("p", None)][QuotaDimension.REQUESTS_PER_DAY] == b


def test_unknown_window_dropped():
    assert normalize([rec(window="week")]) == {}


def test_validate_ok():
    validate_record(rec())
    validate_record(rec(confidence=QuotaConfidence.UNKNOWN, remaining=None, limit=None))
    validate_record(rec(confidence=QuotaConfidence.ESTIMATED, source="estimate:x"))


@pytest.mark.parametrize(
    "bad",
    [
        {"limit": -1},
        {"used": -1},
        {"remaining": -0.5},
        {"limit": float("nan")},
        {"remaining": float("inf")},
        {"limit": True},
        {"source": ""},
        {"observed_at": datetime(2026, 1, 1)},
        {"reset_at": datetime(2026, 1, 1)},
        {"remaining": None},
        {"source": "estimate:x"},
        {"source": "config:x"},
        {"confidence": QuotaConfidence.ESTIMATED, "remaining": None},
    ],
)
def test_validate_rejects(bad):
    with pytest.raises(ValueError):
        validate_record(replace(rec(), **bad))
