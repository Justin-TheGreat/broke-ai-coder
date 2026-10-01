from __future__ import annotations

import math
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum

from app.db.connection import transaction
from app.providers.base import CostClass
from app.timeutil import from_db_opt, to_db


class UsageStatus(StrEnum):
    SUCCESS = "success"
    ERROR = "error"
    UNKNOWN_OUTCOME = "unknown_outcome"


@dataclass(frozen=True, slots=True)
class UsageEvent:
    provider: str
    model: str
    cost_class: CostClass
    status: UsageStatus
    occurred_at: datetime
    task_id: str | None = None
    request_id: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    estimated_cost_usd: float | None = None


@dataclass(frozen=True, slots=True)
class UsageTotals:
    requests: int
    input_tokens: int
    output_tokens: int
    events_missing_tokens: int
    earliest: datetime | None


@dataclass(frozen=True, slots=True)
class PaidSpend:
    total_usd: float
    missing_cost_events: int


def _check_tokens(name: str, value: int | None) -> None:
    if value is None:
        return
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a non-negative int")


def _validate(event: UsageEvent) -> None:
    _check_tokens("input_tokens", event.input_tokens)
    _check_tokens("output_tokens", event.output_tokens)
    cost = event.estimated_cost_usd
    if cost is not None:
        if isinstance(cost, bool) or not isinstance(cost, int | float):
            raise ValueError("estimated_cost_usd must be a number")
        if not math.isfinite(cost) or cost < 0:
            raise ValueError("estimated_cost_usd must be finite and >= 0")
    if event.cost_class == CostClass.PAID and cost is None:
        raise ValueError("PAID usage event requires estimated_cost_usd")
    if not event.provider or not event.model:
        raise ValueError("provider and model must be non-empty")
    if event.occurred_at.tzinfo is None or event.occurred_at.utcoffset() is None:
        raise ValueError("occurred_at must be timezone-aware")


def _utc_midnight(now: datetime) -> datetime:
    n = now.astimezone(UTC)
    return n.replace(hour=0, minute=0, second=0, microsecond=0)


class UsageRepository:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def record(self, event: UsageEvent) -> int:
        _validate(event)
        with transaction(self._conn):
            cur = self._conn.execute(
                "INSERT INTO usage_events (task_id, provider, model, request_id, input_tokens,"
                " output_tokens, estimated_cost_usd, cost_class, status, occurred_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    event.task_id,
                    event.provider,
                    event.model,
                    event.request_id,
                    event.input_tokens,
                    event.output_tokens,
                    event.estimated_cost_usd,
                    event.cost_class.value,
                    event.status.value,
                    to_db(event.occurred_at),
                ),
            )
        assert cur.lastrowid is not None
        return cur.lastrowid

    def totals(
        self, provider: str, *, model: str | None = None, since: datetime, until: datetime
    ) -> UsageTotals:
        sql = (
            "SELECT COUNT(*) AS n, COALESCE(SUM(input_tokens), 0) AS tin,"
            " COALESCE(SUM(output_tokens), 0) AS tout,"
            " COALESCE(SUM(CASE WHEN input_tokens IS NULL OR output_tokens IS NULL"
            " THEN 1 ELSE 0 END), 0) AS missing, MIN(occurred_at) AS earliest"
            " FROM usage_events WHERE provider = ? AND occurred_at >= ? AND occurred_at <= ?"
        )
        params: list[object] = [provider, to_db(since), to_db(until)]
        if model is not None:
            sql += " AND model = ?"
            params.append(model)
        row = self._conn.execute(sql, params).fetchone()
        return _row_to_totals(row)

    def paid_spend(self, *, since: datetime, until: datetime) -> PaidSpend:
        row = self._conn.execute(
            "SELECT COALESCE(SUM(COALESCE(estimated_cost_usd, 0.0)), 0.0) AS total,"
            " COALESCE(SUM(CASE WHEN estimated_cost_usd IS NULL THEN 1 ELSE 0 END), 0) AS missing"
            " FROM usage_events WHERE cost_class = 'PAID' AND occurred_at >= ?"
            " AND occurred_at <= ?",
            (to_db(since), to_db(until)),
        ).fetchone()
        return PaidSpend(total_usd=float(row["total"]), missing_cost_events=int(row["missing"]))

    def daily_totals(self, now: datetime) -> list[tuple[str, str, UsageTotals]]:
        rows = self._conn.execute(
            "SELECT provider, model, COUNT(*) AS n, COALESCE(SUM(input_tokens), 0) AS tin,"
            " COALESCE(SUM(output_tokens), 0) AS tout,"
            " COALESCE(SUM(CASE WHEN input_tokens IS NULL OR output_tokens IS NULL"
            " THEN 1 ELSE 0 END), 0) AS missing, MIN(occurred_at) AS earliest"
            " FROM usage_events WHERE occurred_at >= ? AND occurred_at <= ?"
            " GROUP BY provider, model ORDER BY provider, model",
            (to_db(_utc_midnight(now)), to_db(now)),
        ).fetchall()
        return [(r["provider"], r["model"], _row_to_totals(r)) for r in rows]


def _row_to_totals(row: sqlite3.Row) -> UsageTotals:
    return UsageTotals(
        requests=int(row["n"]),
        input_tokens=int(row["tin"]),
        output_tokens=int(row["tout"]),
        events_missing_tokens=int(row["missing"]),
        earliest=from_db_opt(row["earliest"]),
    )
