from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from datetime import datetime

from app.config.models import AppConfig
from app.db.connection import transaction
from app.providers.base import ModelCapability
from app.router.policy import ModelRoutingStatus, is_allowlisted
from app.timeutil import to_db


def _b(value: bool) -> int:
    return 1 if value else 0


class ProviderModelRepository:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def replace_discovered(
        self,
        config: AppConfig,
        provider: str,
        models: Sequence[ModelCapability],
        now: datetime,
    ) -> None:
        ts = to_db(now)
        with transaction(self._conn):
            for cap in models:
                status = (
                    ModelRoutingStatus.ALLOWED
                    if is_allowlisted(config, provider, cap.model)
                    else ModelRoutingStatus.DISCOVERED_ONLY
                )
                self._conn.execute(
                    "INSERT INTO provider_models (provider, model, routing_status,"
                    " supports_tool_calling, supports_structured_output, supports_vision,"
                    " context_window, max_output_tokens, discovered_at, updated_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
                    " ON CONFLICT(provider, model) DO UPDATE SET"
                    " routing_status = excluded.routing_status,"
                    " supports_tool_calling = excluded.supports_tool_calling,"
                    " supports_structured_output = excluded.supports_structured_output,"
                    " supports_vision = excluded.supports_vision,"
                    " context_window = excluded.context_window,"
                    " max_output_tokens = excluded.max_output_tokens,"
                    " updated_at = excluded.updated_at",
                    (
                        provider,
                        cap.model,
                        status.value,
                        _b(cap.supports_tool_calling),
                        _b(cap.supports_structured_output),
                        _b(cap.supports_vision),
                        cap.context_window,
                        cap.max_output_tokens,
                        ts,
                        ts,
                    ),
                )
            self._conn.execute(
                "DELETE FROM provider_models WHERE provider = ? AND updated_at <> ?",
                (provider, ts),
            )

    def allowed_capabilities(self, config: AppConfig, provider: str) -> list[ModelCapability]:
        rows = self._conn.execute(
            "SELECT * FROM provider_models WHERE provider = ? ORDER BY model", (provider,)
        ).fetchall()
        return [
            ModelCapability(
                provider=provider,
                model=r["model"],
                supports_tool_calling=bool(r["supports_tool_calling"]),
                supports_structured_output=bool(r["supports_structured_output"]),
                supports_vision=bool(r["supports_vision"]),
                context_window=r["context_window"],
                max_output_tokens=r["max_output_tokens"],
            )
            for r in rows
            if is_allowlisted(config, provider, r["model"])
        ]
