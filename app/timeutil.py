from __future__ import annotations

from datetime import UTC, datetime


def utcnow() -> datetime:
    return datetime.now(UTC)


def to_db(dt: datetime) -> str:
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise ValueError("naive datetime not allowed; use an aware datetime")
    return dt.astimezone(UTC).isoformat(timespec="microseconds")


def from_db(value: str) -> datetime:
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def to_db_opt(dt: datetime | None) -> str | None:
    return None if dt is None else to_db(dt)


def from_db_opt(value: str | None) -> datetime | None:
    return None if value is None else from_db(value)
