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


def test_once_subprocess_needs_no_opencode_binary(tmp_path):
    import subprocess
    import sys

    cfg = tmp_path / "c.yaml"
    cfg.write_text(
        f"database:\n  path: {(tmp_path / 'x.db').as_posix()}\nopencode:\n  managed_server: false\n"
    )
    r = subprocess.run(
        [sys.executable, "-m", "app", "--config", str(cfg), "--once"],
        cwd=ROOT,
        timeout=60,
        capture_output=True,
    )
    assert r.returncode == 0, r.stderr


def test_startup_error_prints_message_and_returns_3(tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("DISCORD_BOT_TOKEN", raising=False)
    cfg = tmp_path / "c.yaml"
    cfg.write_text(
        f"database:\n  path: {(tmp_path / 'x.db').as_posix()}\n"
        "opencode:\n  command: [no-such-opencode-binary-xyz]\n  os_user: {mode: none}\n"
    )
    assert main(["--config", str(cfg)]) == 3
    err = capsys.readouterr().err
    assert "opencode executable not found" in err
    assert "Traceback" not in err


# ---- --print-sudoers / --check-os-user ------------------------------------------------------


def _cfg(tmp_path, command="/usr/local/bin/opencode", mode="sudo"):
    p = tmp_path / "c.yaml"
    p.write_text(
        f"database:\n  path: {(tmp_path / 'x.db').as_posix()}\n"
        f"opencode:\n  command: [{command}]\n  os_user: {{mode: {mode}}}\n"
    )
    return str(p)


def _fake_posix(monkeypatch):
    import app.cli as cli
    from app.opencode.os_user import SystemHostProbe, UserInfo

    monkeypatch.setattr(cli.os, "name", "posix", raising=False)
    monkeypatch.setattr(cli.os, "geteuid", lambda: 1000, raising=False)
    monkeypatch.setattr(
        SystemHostProbe,
        "user_by_uid",
        lambda self, uid: UserInfo("justin", 1000, 1000, "/home/justin"),
    )


def test_print_sudoers_prints_exact_text(tmp_path, capsys, monkeypatch):
    from app.opencode.os_user import render_sudoers

    _fake_posix(monkeypatch)
    assert main(["--config", _cfg(tmp_path), "--print-sudoers"]) == 0
    out = capsys.readouterr().out
    assert out == render_sudoers("justin", "brokeoc", "/usr/local/bin/opencode")


def test_print_sudoers_relative_command_exits_2(tmp_path, capsys, monkeypatch):
    _fake_posix(monkeypatch)
    assert main(["--config", _cfg(tmp_path, command="opencode"), "--print-sudoers"]) == 2
    captured = capsys.readouterr()
    assert captured.out == "" and "absolute path" in captured.err


def test_print_sudoers_needs_posix(tmp_path, capsys, monkeypatch):
    import app.cli as cli

    monkeypatch.setattr(cli.os, "name", "nt", raising=False)
    assert main(["--config", _cfg(tmp_path), "--print-sudoers"]) == 2
    captured = capsys.readouterr()
    assert captured.out == "" and "inside WSL" in captured.err


def test_check_os_user_mode_none_exits_3(tmp_path, capsys):
    assert main(["--config", _cfg(tmp_path, mode="none"), "--check-os-user"]) == 3
    assert "unsupported for production" in capsys.readouterr().err


def test_check_os_user_ok_runs_self_check_only(tmp_path, capsys, monkeypatch):
    from app.opencode.os_user import SudoLauncher

    calls: list[str] = []

    async def fake_check(self):
        calls.append("self_check")

    async def no_sweep(self):
        raise AssertionError("--check-os-user must never sweep")

    monkeypatch.setattr(SudoLauncher, "self_check", fake_check)
    monkeypatch.setattr(SudoLauncher, "sweep", no_sweep)
    assert main(["--config", _cfg(tmp_path), "--check-os-user"]) == 0
    assert calls == ["self_check"]
    assert (
        "os_user OK: OpenCode runs as brokeoc; isolation checks passed" in capsys.readouterr().out
    )


def test_check_os_user_failure_exits_3(tmp_path, capsys, monkeypatch):
    from app.opencode.os_user import SudoLauncher
    from app.runtime.interfaces import StartupError

    async def fail(self):
        raise StartupError("OpenCode user isolation (opencode.os_user) check failed: x")

    monkeypatch.setattr(SudoLauncher, "self_check", fail)
    assert main(["--config", _cfg(tmp_path), "--check-os-user"]) == 3
    assert "check failed: x" in capsys.readouterr().err
