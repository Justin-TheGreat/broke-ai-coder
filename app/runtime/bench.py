"""Idle benchmark for the controller daemon (T024).

Measures RSS and CPU of THIS process only (controller + gateway, no Discord connection, no
OpenCode, no child processes) against the idle targets of ``app/runtime/resources.py``. It uses a
temporary database, an empty environment (no key is ever read) and makes no network calls.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import tempfile
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from app.config.loader import ConfigError, load_config
from app.config.models import AppConfig
from app.redaction import SecretRedactor
from app.runtime.resources import (
    IDLE_CPU_PCT,
    IDLE_RSS_MB,
    NORMAL_CPU_PCT,
    NORMAL_RSS_MB,
    ProcessSampler,
)
from app.runtime.wiring import build_runtime, close_runtime

_REPO = Path(__file__).resolve().parents[2]


@dataclass(frozen=True, slots=True)
class BenchResult:
    duration_s: float
    interval_s: float
    samples: int
    rss_max_bytes: int | None
    rss_last_bytes: int | None
    cpu_avg_percent: float | None
    cpu_max_interval_percent: float | None


async def run_idle_benchmark(
    config: AppConfig,
    *,
    seconds: float,
    interval: float,
    warmup: float,
    import_discord: bool,
    sampler_factory: Callable[[], ProcessSampler] = ProcessSampler,
) -> BenchResult:
    with tempfile.TemporaryDirectory(prefix="broke-bench-") as tmp:
        db = config.database.model_copy(update={"path": str(Path(tmp) / "bench.db")})
        config = config.model_copy(update={"database": db})
        if import_discord:
            import app.bot.discord_frontend  # noqa: F401  (module memory only; never connects)
        rt = build_runtime(config, redactor=SecretRedactor(), environ={}, once=True)
        try:
            await rt.controller.start()
            await rt.gateway.start()
            await asyncio.sleep(max(0.0, warmup))
            sampler = sampler_factory()
            start = time.monotonic()
            first = sampler.sample()
            rss_max = first.rss_bytes
            rss_last = first.rss_bytes
            window_max: float | None = None
            samples = 1
            last = first
            while True:
                remaining = seconds - (time.monotonic() - start)
                if remaining <= 0:
                    break
                await asyncio.sleep(min(interval, remaining))
                last = sampler.sample()
                samples += 1
                if last.rss_bytes is not None:
                    rss_last = last.rss_bytes
                    rss_max = last.rss_bytes if rss_max is None else max(rss_max, last.rss_bytes)
                if last.cpu_percent_window is not None:
                    window_max = (
                        last.cpu_percent_window
                        if window_max is None
                        else max(window_max, last.cpu_percent_window)
                    )
            cpu_avg = None
            wall = last.uptime_s - first.uptime_s
            if samples > 1 and wall >= 0.5:
                cpu_avg = 100.0 * (last.cpu_seconds_total - first.cpu_seconds_total) / wall
            return BenchResult(
                duration_s=time.monotonic() - start,
                interval_s=interval,
                samples=samples,
                rss_max_bytes=rss_max,
                rss_last_bytes=rss_last,
                cpu_avg_percent=cpu_avg,
                cpu_max_interval_percent=window_max,
            )
        finally:
            try:
                await rt.controller.stop()
            finally:
                await close_runtime(rt)


def _verdict(value: float | None, limit: float) -> str:
    if value is None:
        return "UNKNOWN"
    return "PASS" if value < limit else "FAIL"


def _verdicts(result: BenchResult) -> tuple[str, str]:
    rss_mb = None if result.rss_max_bytes is None else result.rss_max_bytes / 1_048_576
    return _verdict(rss_mb, IDLE_RSS_MB), _verdict(result.cpu_avg_percent, IDLE_CPU_PCT)


def format_report(result: BenchResult) -> str:
    rss_v, cpu_v = _verdicts(result)
    rss = (
        "unavailable on this platform"
        if result.rss_max_bytes is None
        else f"{result.rss_max_bytes / 1_048_576:.1f} MB"
    )
    last = "n/a" if result.rss_last_bytes is None else f"{result.rss_last_bytes / 1_048_576:.1f} MB"
    avg = "n/a" if result.cpu_avg_percent is None else f"{result.cpu_avg_percent:.2f}%"
    peak = (
        "n/a"
        if result.cpu_max_interval_percent is None
        else f"{result.cpu_max_interval_percent:.2f}%"
    )
    return "\n".join(
        [
            "Idle benchmark (controller daemon only; excludes OpenCode and child processes)",
            f"Measured {result.duration_s:.1f} s, {result.samples} samples,"
            f" interval {result.interval_s:g} s",
            f"[{rss_v}] RSS max {rss} (last {last}); idle target < {IDLE_RSS_MB} MB",
            f"[{cpu_v}] CPU avg {avg} of one core; idle target < {IDLE_CPU_PCT:g}%",
            f"[INFO] CPU max over one interval {peak}",
            f"Normal-load targets for reference: RSS < {NORMAL_RSS_MB} MB,"
            f" CPU < {NORMAL_CPU_PCT:g}%",
        ]
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="bench_idle", description=__doc__)
    parser.add_argument("--seconds", type=float, default=1800.0)
    parser.add_argument("--interval", type=float, default=5.0)
    parser.add_argument("--warmup", type=float, default=5.0)
    parser.add_argument("--config", default=None)
    parser.add_argument("--no-discord-import", action="store_true")
    args = parser.parse_args(argv)
    if args.seconds <= 0 or args.interval <= 0 or args.warmup < 0:
        print("--seconds and --interval must be positive, --warmup non-negative", file=sys.stderr)
        return 2
    config_path = args.config
    if config_path is None:
        config_path = (
            "config.yaml" if Path("config.yaml").is_file() else _REPO / "config.example.yaml"
        )
    try:
        config = load_config(config_path)
    except ConfigError as e:
        print(str(e), file=sys.stderr)
        return 2
    result = asyncio.run(
        run_idle_benchmark(
            config,
            seconds=args.seconds,
            interval=args.interval,
            warmup=args.warmup,
            import_discord=not args.no_discord_import,
        )
    )
    print(format_report(result))
    return 1 if "FAIL" in _verdicts(result) else 0
