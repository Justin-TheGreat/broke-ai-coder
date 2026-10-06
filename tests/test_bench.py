"""T024 idle benchmark: offline, temp database, no secrets read."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from app.runtime.bench import BenchResult, format_report, main

ROOT = Path(__file__).resolve().parent.parent
EXAMPLE = str(ROOT / "config.example.yaml")


def test_main_runs_and_labels_scope(capsys):
    rc = main(
        [
            "--seconds",
            "0.3",
            "--interval",
            "0.1",
            "--warmup",
            "0",
            "--no-discord-import",
            "--config",
            EXAMPLE,
        ]
    )
    out = capsys.readouterr().out
    assert rc in (0, 1)
    assert "excludes OpenCode" in out


def test_main_bad_config_returns_2(capsys, tmp_path):
    assert main(["--config", str(tmp_path / "missing.yaml")]) == 2
    assert "not found" in capsys.readouterr().err


def test_report_verdicts():
    good = BenchResult(10.0, 1.0, 5, 100 * 1_048_576, 90 * 1_048_576, 0.5, 1.0)
    assert "[PASS] RSS" in format_report(good) and "[PASS] CPU" in format_report(good)
    bad = BenchResult(10.0, 1.0, 5, 300 * 1_048_576, 300 * 1_048_576, 5.0, 9.0)
    assert "[FAIL] RSS" in format_report(bad) and "[FAIL] CPU" in format_report(bad)
    unknown = BenchResult(10.0, 1.0, 5, None, None, None, None)
    text = format_report(unknown)
    assert "[UNKNOWN] RSS" in text and "[UNKNOWN] CPU" in text
    assert text.splitlines()[0].endswith("excludes OpenCode and child processes)")


def test_script_subprocess_leaks_nothing():
    sentinel = "SENTINEL-DISCORD-TOKEN-9f3a1c77"
    data_dir = ROOT / "data"
    before = set(data_dir.glob("*.db")) if data_dir.exists() else set()
    env = dict(os.environ)
    env["DISCORD_BOT_TOKEN"] = sentinel
    proc = subprocess.run(
        [
            sys.executable,
            "scripts/bench_idle.py",
            "--seconds",
            "0.5",
            "--interval",
            "0.1",
            "--warmup",
            "0",
        ],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode in (0, 1), proc.stderr
    assert sentinel not in proc.stdout + proc.stderr
    after = set(data_dir.glob("*.db")) if data_dir.exists() else set()
    assert after == before
