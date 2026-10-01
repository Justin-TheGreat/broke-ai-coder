from __future__ import annotations

from pathlib import Path

from app.cli import main

ROOT = Path(__file__).resolve().parent.parent


def test_check_example_config(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert main(["--config", str(ROOT / "config.example.yaml"), "--check"]) == 0
    assert "config OK" in capsys.readouterr().out


def test_missing_config_returns_2(tmp_path):
    assert main(["--config", str(tmp_path / "nope.yaml"), "--check"]) == 2


def test_invalid_config_returns_2(tmp_path):
    p = tmp_path / "bad.yaml"
    p.write_text("bogus: 1\n")
    assert main(["--config", str(p), "--check"]) == 2


def test_once_subprocess(tmp_path):
    import subprocess
    import sys

    cfg = tmp_path / "c.yaml"
    cfg.write_text(f"database:\n  path: {(tmp_path / 'x.db').as_posix()}\n")
    r = subprocess.run(
        [sys.executable, "-m", "app", "--config", str(cfg), "--once"],
        cwd=ROOT,
        timeout=60,
        capture_output=True,
    )
    assert r.returncode == 0, r.stderr
