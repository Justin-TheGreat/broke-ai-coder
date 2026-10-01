from __future__ import annotations

import logging
import math
from collections import deque
from collections.abc import Callable
from datetime import datetime, timedelta

from app.config.models import CooldownConfig
from app.providers.base import ErrorClass
from app.timeutil import utcnow

logger = logging.getLogger(__name__)


class CooldownManager:
    def __init__(self, config: CooldownConfig, *, clock: Callable[[], datetime] = utcnow) -> None:
        self._config = config
        self._clock = clock
        self._until: dict[tuple[str, str | None], datetime] = {}
        self._net_failures: dict[str, deque[datetime]] = {}

    def _apply(
        self,
        key: tuple[str, str | None],
        until: datetime,
        reason: str,
    ) -> datetime:
        existing = self._until.get(key)
        if existing is not None and existing >= until:
            return existing
        self._until[key] = until
        logger.info(
            "cooldown provider=%s model=%s until=%s reason=%s",
            key[0],
            key[1],
            until.isoformat(),
            reason,
        )
        return until

    def _duration(self, retry_after_s: float | None, default: float) -> float:
        d = default
        if retry_after_s is not None and math.isfinite(retry_after_s):
            d = retry_after_s
        return min(max(d, 0.0), self._config.max_cooldown_s)

    def record_failure(
        self,
        provider: str,
        model: str | None,
        error_class: ErrorClass,
        *,
        retry_after_s: float | None = None,
        now: datetime | None = None,
    ) -> datetime | None:
        now = now if now is not None else self._clock()
        cfg = self._config
        if error_class in (ErrorClass.RATE_LIMITED, ErrorClass.QUOTA_EXHAUSTED):
            key = (provider, model)
            d = self._duration(retry_after_s, cfg.rate_limit_default_s)
        elif error_class == ErrorClass.PROVIDER_UNAVAILABLE:
            key = (provider, None)
            d = self._duration(retry_after_s, cfg.provider_unavailable_s)
        elif error_class in (ErrorClass.TRANSIENT_NETWORK, ErrorClass.TIMEOUT_UNKNOWN_OUTCOME):
            failures = self._net_failures.setdefault(
                provider, deque(maxlen=cfg.network_failure_threshold)
            )
            failures.append(now)
            cutoff = now - timedelta(seconds=cfg.network_failure_window_s)
            while failures and failures[0] <= cutoff:
                failures.popleft()
            if len(failures) < cfg.network_failure_threshold:
                return None
            failures.clear()
            key = (provider, None)
            d = self._duration(retry_after_s, cfg.network_failure_cooldown_s)
        else:
            return None
        if d <= 0:
            return None
        return self._apply(key, now + timedelta(seconds=d), str(error_class))

    def record_success(self, provider: str, *, now: datetime | None = None) -> None:
        failures = self._net_failures.get(provider)
        if failures is not None:
            failures.clear()

    def set_cooldown(self, provider: str, model: str | None, until: datetime) -> None:
        self._apply((provider, model), until, "manual")

    def active(self, now: datetime | None = None) -> dict[tuple[str, str | None], datetime]:
        now = now if now is not None else self._clock()
        for key in [k for k, u in self._until.items() if u <= now]:
            del self._until[key]
        return dict(self._until)

    def is_cooling(self, provider: str, model: str | None, now: datetime | None = None) -> bool:
        active = self.active(now)
        return (provider, None) in active or (provider, model) in active
