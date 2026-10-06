"""Process self-metrics (T024), stdlib only. Daemon process only: no OpenCode, no children."""

from __future__ import annotations

import os
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass

IDLE_RSS_MB = 250
NORMAL_RSS_MB = 512
IDLE_CPU_PCT = 2.0
NORMAL_CPU_PCT = 10.0

_MIN_UPTIME_S = 0.5
_MIN_WINDOW_S = 1.0


def read_rss_bytes(status_path: str = "/proc/self/status") -> int | None:
    """VmRSS (kB * 1024) from /proc; None if missing or unparseable (Windows, macOS)."""
    try:
        with open(status_path, encoding="ascii", errors="replace") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    parts = line.split()
                    return int(parts[1]) * 1024
    except (OSError, ValueError, IndexError):
        return None
    return None


def read_peak_rss_bytes() -> int | None:
    """Linux only: ru_maxrss (kB) * 1024; None where ``resource`` is missing or not Linux."""
    try:
        if not sys.platform.startswith("linux"):
            return None
        import resource

        return int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss) * 1024
    except Exception:
        return None


def cpu_seconds() -> float:
    """User + system CPU seconds of this process."""
    try:
        t = os.times()
        return float(t.user + t.system)
    except Exception:
        return 0.0


@dataclass(frozen=True, slots=True)
class ResourceSample:
    rss_bytes: int | None
    peak_rss_bytes: int | None
    cpu_seconds_total: float
    uptime_s: float
    cpu_percent_since_start: float | None
    cpu_percent_window: float | None
    window_s: float


class ProcessSampler:
    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        cpu: Callable[[], float] = cpu_seconds,
        rss: Callable[[], int | None] = read_rss_bytes,
        peak: Callable[[], int | None] = read_peak_rss_bytes,
    ) -> None:
        self._clock = clock
        self._cpu = cpu
        self._rss = rss
        self._peak = peak
        self._start_wall = clock()
        self._start_cpu = self._safe_cpu()
        self._prev_wall = self._start_wall
        self._prev_cpu = self._start_cpu

    def _safe_cpu(self) -> float:
        try:
            return float(self._cpu())
        except Exception:
            return 0.0

    @staticmethod
    def _safe(reader: Callable[[], int | None]) -> int | None:
        try:
            return reader()
        except Exception:
            return None

    def sample(self) -> ResourceSample:
        wall = self._clock()
        cpu = self._safe_cpu()
        uptime = max(0.0, wall - self._start_wall)
        window = max(0.0, wall - self._prev_wall)
        since_start = 100.0 * (cpu - self._start_cpu) / uptime if uptime >= _MIN_UPTIME_S else None
        in_window = 100.0 * (cpu - self._prev_cpu) / window if window >= _MIN_WINDOW_S else None
        sample = ResourceSample(
            rss_bytes=self._safe(self._rss),
            peak_rss_bytes=self._safe(self._peak),
            cpu_seconds_total=cpu,
            uptime_s=uptime,
            cpu_percent_since_start=since_start,
            cpu_percent_window=in_window,
            window_s=window,
        )
        self._prev_wall, self._prev_cpu = wall, cpu
        return sample
