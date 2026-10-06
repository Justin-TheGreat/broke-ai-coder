"""Optional probes for /health (T093). Each probe is a cheap, secret-free callable."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from app.runtime.resources import ProcessSampler


@dataclass(slots=True)
class RuntimeProbes:
    sampler: ProcessSampler = field(default_factory=ProcessSampler)
    gateway_up: Callable[[], bool] | None = None
    serve_state: Callable[[], tuple[bool, str | None]] | None = None  # (ready, fatal_error)
    os_user_summary: Callable[[], str] | None = None
