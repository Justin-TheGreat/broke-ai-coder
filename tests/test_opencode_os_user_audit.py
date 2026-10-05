"""Tester audit additions for T072: end-to-end sudo argv/env through the real executor and
serve manager, real-probe helpers (interop, killpg, spawn kwargs), per-group and startup checks.

No real sudo, network or Discord. A fake ``create_subprocess_exec`` captures what would run.
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
from pathlib import Path

import httpx
import pytest
from test_opencode_os_user import (
    EXE,
    FakeHostProbe,
    launcher,
)

import app.opencode.os_user as ou
from app.cli import main
from app.config.models import OpenCodeConfig
from app.opencode.executor import OpenCodeExecutor
from app.opencode.os_user import ISOLATION_FAILED, KEEP_ENV, PKILL, SUDO, SudoLauncher
from app.opencode.server import OpenCodeServerManager
from app.runtime.interfaces import ExecutionOutcome, StartupError

SECRETS = {
    "OPENROUTER_API_KEY": "sk-or-SECRETVALUE1",
    "GROQ_API_KEY": "gsk-SECRETVALUE2",
    "DISCORD_BOT_TOKEN": "discord-SECRETVALUE3",
    "AWS_SECRET_ACCESS_KEY": "aws-SECRETVALUE4",
    "HOME": "/home/justin",
    "XDG_DATA_HOME": "/home/justin/.local/share",
    "XDG_CONFIG_HOME": "/home/justin/.config",
    "LANG": "C.UTF-8",
    "PATH": "/home/justin/bin:/usr/bin",
}


class _Stop(Exception):
    pass


def _capture_spawn(monkeypatch) -> dict:
    seen: dict = {"calls": 0}

    async def fake(*argv, **kw):
        seen["calls"] += 1
        seen["argv"], seen["kw"] = list(argv), kw
        raise OSError("captured")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake)
    return seen


def _sudo_executor(make_config, *, reason=None, extra_env=None):
    sl, probe, cfg = launcher(make_config)
    ex = OpenCodeExecutor(
        cfg.opencode,
        launcher=sl,
        environ=SECRETS,
        extra_env={
            "OPENCODE_CONFIG_CONTENT": "{}",
            "OPENCODE_SERVER_PASSWORD": "pw",
            "OPENCODE_SERVER_USERNAME": "opencode",
            "FOO": "bar",
            **(extra_env or {}),
        },
        unavailable_reason=reason,
        transport=httpx.MockTransport(lambda r: httpx.Response(200)),
    )
    return ex, sl, probe


async def _run(ex, tmp_path, prompt="do it"):
    return await ex.run(
        task_id="t",
        provider="groq",
        model="m",
        prompt=prompt,
        session_id=None,
        project_dir=str(tmp_path),
        cancel_event=asyncio.Event(),
    )


# ---- real executor through SudoLauncher: argv, env, spawn kwargs ------------------------


@pytest.mark.parametrize(
    "prompt",
    ["-x --help", "--dangerously-skip", "; rm -rf ~ $(id) `id`\nnext", "-u root -- /bin/sh"],
)
async def test_executor_sudo_argv_prompt_is_single_element_after_double_dash(
    make_config, tmp_path, monkeypatch, prompt
):
    seen = _capture_spawn(monkeypatch)
    ex, _, _ = _sudo_executor(make_config)
    r = await _run(ex, tmp_path, prompt)
    assert r.outcome == ExecutionOutcome.EXECUTOR_UNAVAILABLE  # our fake spawn raised OSError
    argv = seen["argv"]
    assert argv[:5] == [SUDO, "-n", "-u", "brokeoc", "--"]
    assert argv[5] == EXE  # absolute path directly after --, nothing else a sudo option
    assert argv.count("--") == 1 or argv.index("--") == 4
    assert argv[-1].strip() == prompt.strip()
    # the user-controlled element is last and never before the first "--"
    assert argv.index(argv[-1]) > 4
    assert not any(a in ("-E", "--preserve-env", "-s", "-i", "-S", "-A") for a in argv[:5])
    assert "root" not in argv[:5]
    assert not any(p in ("sh", "bash", "-c") for p in argv[:6])


async def test_executor_sudo_env_has_no_secrets_and_exact_allowlist(
    make_config, tmp_path, monkeypatch
):
    seen = _capture_spawn(monkeypatch)
    ex, _, _ = _sudo_executor(make_config)
    await _run(ex, tmp_path)
    env = seen["kw"]["env"]
    assert set(env) == {"PATH", "LANG", *[k for k in KEEP_ENV if k != "LANG"]}
    assert env["PATH"] == "/usr/bin:/bin"
    blob = repr(env)
    for secret in SECRETS.values():
        if secret not in ("C.UTF-8", "/usr/bin:/bin"):
            assert secret not in blob
    assert "FOO" not in env


async def test_executor_sudo_spawn_kwargs_no_stdin_no_tty_no_preserve(
    make_config, tmp_path, monkeypatch
):
    seen = _capture_spawn(monkeypatch)
    ex, _, _ = _sudo_executor(make_config)
    await _run(ex, tmp_path)
    kw = seen["kw"]
    assert kw["stdin"] == subprocess.DEVNULL
    assert kw.get("close_fds", True) is True
    if sys.platform != "win32":
        assert kw["start_new_session"] is True
    assert kw["cwd"] == str(tmp_path)  # project dir is cwd, never an argv element


async def test_n9_reason_flips_mid_task_second_attempt_does_not_spawn(
    make_config, tmp_path, monkeypatch
):
    state = {"reason": None}
    seen = _capture_spawn(monkeypatch)
    ex, _, _ = _sudo_executor(make_config, reason=lambda: state["reason"])
    await _run(ex, tmp_path)  # attempt 1 spawns (and our fake fails it)
    assert seen["calls"] == 1
    state["reason"] = "managed OpenCode server is down: restarting"
    r = await _run(ex, tmp_path)
    assert r.outcome == ExecutionOutcome.EXECUTOR_UNAVAILABLE
    assert r.detail == "managed OpenCode server is down: restarting"
    assert seen["calls"] == 1  # nothing spawned during the second attempt


async def test_executor_sudo_rejects_relative_command_without_spawn(
    make_config, tmp_path, monkeypatch
):
    seen = _capture_spawn(monkeypatch)
    sl, _, _ = launcher(make_config)
    ex = OpenCodeExecutor(
        OpenCodeConfig(command=["opencode"]),
        launcher=sl,
        environ={},
        transport=httpx.MockTransport(lambda r: httpx.Response(200)),
    )
    r = await _run(ex, tmp_path)
    assert r.outcome == ExecutionOutcome.EXECUTOR_UNAVAILABLE
    assert seen["calls"] == 0


# ---- real serve manager through SudoLauncher --------------------------------------------


class _GW:
    base_url = "http://127.0.0.1:1/v1"
    token = "gw-token-0123456789"


async def test_serve_spawn_under_sudo_argv_env_cwd(make_config, tmp_path, monkeypatch):
    seen = _capture_spawn(monkeypatch)
    sl, _, cfg = launcher(make_config)
    m = OpenCodeServerManager(
        cfg,
        gateway=_GW(),  # type: ignore[arg-type]
        environ=SECRETS,
        password="serve-pw-0123456789",
        launcher=sl,
    )
    m._executable = sl.executable(cfg.opencode.command[0])
    with pytest.raises(Exception):  # noqa: B017 - _ServeFailed from the captured OSError
        await m._spawn()
    argv, kw = seen["argv"], seen["kw"]
    assert argv[:6] == [SUDO, "-n", "-u", "brokeoc", "--", EXE]
    assert "serve" in argv and argv[argv.index("--hostname") + 1] == "127.0.0.1"
    env = kw["env"]
    assert set(env) == {
        "PATH",
        "LANG",
        "OPENCODE_CONFIG_CONTENT",
        "OPENCODE_SERVER_PASSWORD",
        "OPENCODE_SERVER_USERNAME",
    }
    for secret in ("SECRETVALUE", "/home/justin"):
        assert secret not in repr(env)
    assert kw["stdin"] == subprocess.DEVNULL
    assert kw["cwd"] == cfg.opencode.server_directory


# ---- self-check: each privileged group, root-ish uid, custom names ----------------------


@pytest.mark.parametrize(
    "group", ["root", "sudo", "admin", "wheel", "adm", "docker", "lxd", "disk", "shadow"]
)
async def test_each_privileged_group_fails_closed(make_config, group):
    probe = FakeHostProbe()
    probe.groups = frozenset({"brokeoc", group})
    sl, _, _ = launcher(make_config, probe)
    with pytest.raises(StartupError) as ei:
        await sl.self_check()
    assert str(ei.value).startswith(ISOLATION_FAILED) and group in str(ei.value)
    assert probe.pkills == []


async def test_daemon_home_trailing_slash_command_inside_home_rejected(make_config):
    probe = FakeHostProbe()
    probe.users["justin"] = type(probe.users["justin"])("justin", 1000, 1000, "/home/justin/")
    sl, _, _ = launcher(make_config, probe, command=["/home/justin/oc/opencode"])
    with pytest.raises(StartupError, match="must not be inside the daemon user's home"):
        await sl.self_check()


async def test_home_prefix_sibling_is_not_inside_home(make_config):
    # /home/justin2 must not be treated as under /home/justin
    probe = FakeHostProbe()
    sl, _, _ = launcher(make_config, probe, server_directory="/home/justin2/serve")
    probe.set(["/usr/bin/test", "-w", "/home/justin2/serve"], ou.ProbeResult(0))
    await sl.self_check()


async def test_self_check_stops_at_first_failed_access_probe(make_config):
    probe = FakeHostProbe()
    probe.set(["/usr/bin/test", "-r", "/home/justin"], ou.ProbeResult(0))
    sl, _, _ = launcher(make_config, probe)
    with pytest.raises(StartupError):
        await sl.self_check()
    # nothing after the first failing probe ran (fail closed, no later probes)
    assert probe.runs[-1][5:] == ["/usr/bin/test", "-r", "/home/justin"]


async def test_self_check_error_messages_contain_no_env_secrets(make_config, monkeypatch):
    for k, v in SECRETS.items():
        if k.endswith(("KEY", "TOKEN")):
            monkeypatch.setenv(k, v)
    probe = FakeHostProbe()
    probe.interop = True
    sl, _, _ = launcher(make_config, probe)
    with pytest.raises(StartupError) as ei:
        await sl.self_check()
    assert "SECRETVALUE" not in str(ei.value)


# ---- SystemHostProbe helpers against a fake filesystem / os -----------------------------


def _binfmt(tmp_path: Path, monkeypatch, entries: dict[str, str | None]) -> None:
    d = tmp_path / "binfmt"
    d.mkdir()
    if "status" not in entries:
        (d / "status").write_text("enabled\n")  # binfmt_misc is mounted
    for name, content in entries.items():
        if content is not None:
            (d / name).write_text(content)
    monkeypatch.setattr(ou, "_BINFMT_DIR", str(d))


@pytest.mark.parametrize(
    ("entries", "expected"),
    [
        ({"WSLInterop": "enabled\ninterpreter /init\n"}, True),
        ({"WSLInterop-late": "enabled\ninterpreter /init\n"}, True),
        ({"WSLInterop": "disabled\n", "WSLInterop-late": "enabled\n"}, True),
        ({"WSLInterop": "enabled\n", "WSLInterop-late": "disabled\n"}, True),
        ({"WSLInterop": "disabled\n", "WSLInterop-late": "disabled\n"}, False),
        ({"WSLInterop-late": "disabled\n"}, False),
        ({"python3.12": "enabled\n"}, False),  # unrelated binfmt entries are ignored
        ({"WSLInterop": ""}, True),  # empty/garbled first line is not "disabled": fail closed
        ({"WSLInterop": "garbage\n"}, True),
        ({}, False),
    ],
)
def test_interop_enabled_reads_binfmt_entries(tmp_path, monkeypatch, entries, expected):
    _binfmt(tmp_path, monkeypatch, entries)
    assert ou.SystemHostProbe().interop_enabled() is expected


def test_interop_unreadable_entry_fails_closed(tmp_path, monkeypatch):
    _binfmt(tmp_path, monkeypatch, {"WSLInterop": "disabled\n"})
    real_open = Path.open

    def boom(self, *a, **k):
        if self.name.startswith("WSLInterop"):
            raise PermissionError
        return real_open(self, *a, **k)

    monkeypatch.setattr(Path, "open", boom)
    assert ou.SystemHostProbe().interop_enabled() is True


def test_system_killpg_names_and_guards(monkeypatch):
    calls: list[tuple[int, int]] = []
    monkeypatch.setattr(ou.os, "killpg", lambda p, s: calls.append((p, s)), raising=False)
    monkeypatch.setattr(ou.signal, "SIGTERM", 15, raising=False)
    monkeypatch.setattr(ou.signal, "SIGKILL", 9, raising=False)
    probe = ou.SystemHostProbe()
    probe.killpg(123, "TERM")
    probe.killpg(123, "KILL")
    probe.killpg(0, "KILL")  # never signal our own group
    probe.killpg(-5, "KILL")
    probe.killpg(123, "NOPE")
    assert calls == [(123, 15), (123, 9)]


@pytest.mark.parametrize("exc", [ProcessLookupError, PermissionError, OSError])
def test_system_killpg_never_raises(monkeypatch, exc):
    def boom(p, s):
        raise exc

    monkeypatch.setattr(ou.os, "killpg", boom, raising=False)
    monkeypatch.setattr(ou.signal, "SIGTERM", 15, raising=False)
    ou.SystemHostProbe().killpg(5, "TERM")


async def test_system_probe_spawn_kwargs_exact(monkeypatch):
    seen: dict = {}

    class P:
        returncode = 0

        async def communicate(self):
            return b"", b""

    async def fake(*argv, **kw):
        seen["kw"] = kw
        return P()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake)
    await ou.SystemHostProbe().run(["x"], timeout_s=1)
    kw = seen["kw"]
    assert kw["stdin"] == subprocess.DEVNULL
    assert kw["stdout"] == subprocess.PIPE and kw["stderr"] == subprocess.PIPE
    assert kw["env"] == {"PATH": "/usr/bin:/bin"}
    assert kw["start_new_session"] is True
    assert kw.get("close_fds", True) is True
    assert kw.get("shell", False) is False


# ---- kill paths: pkill always sudo-as-oc-user, never targets the daemon's user ----------


async def test_every_pkill_goes_through_sudo_as_oc_user_and_names_session_or_user(make_config):
    sl, probe, _ = launcher(make_config)
    await sl.kill_tree(FakeProcLike(900), grace_s=0.05)  # type: ignore[arg-type]
    await sl.sweep()
    assert probe.runs, "expected pkill invocations"
    for r in probe.runs:
        assert r[:5] == [SUDO, "-n", "-u", "brokeoc", "--"] and r[5] == PKILL
        args = r[6:]
        # only ever selects by session id of our launch or by the OpenCode user's uid name
        assert (args[1] == "-s" and args[2] == "900") or args[1:] == ["-U", "brokeoc"]
        assert "justin" not in args and "-u" not in args and "-a" not in args
    assert (-1, "KILL") not in probe.killpgs and all(p > 0 for p, _ in probe.killpgs)


class FakeProcLike:
    def __init__(self, pid):
        self.pid = pid
        self.returncode = None

    async def wait(self):
        self.returncode = -15
        return -15

    def kill(self):
        self.returncode = -9


# ---- startup: exit 3, fail closed before anything else runs, no secrets -----------------


def _sudo_cfg(tmp_path) -> str:
    p = tmp_path / "c.yaml"
    p.write_text(
        f"database:\n  path: {(tmp_path / 'x.db').as_posix()}\n"
        "opencode:\n  command: [/usr/local/bin/opencode]\n  os_user: {mode: sudo}\n"
    )
    return str(p)


def test_startup_exits_3_when_isolation_check_fails_no_secrets_no_sweep(
    tmp_path, capsys, monkeypatch
):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-SECRETVALUE1")
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "discord-SECRETVALUE3")
    monkeypatch.setattr(ou.SystemHostProbe, "platform", lambda self: "win32")

    async def no_sweep(self):
        raise AssertionError("a failed self-check must never sweep")

    async def no_run(self, argv, *, timeout_s, on_timeout=None):
        raise AssertionError("no probe may run when the platform check fails first")

    monkeypatch.setattr(SudoLauncher, "sweep", no_sweep)
    monkeypatch.setattr(ou.SystemHostProbe, "run", no_run)
    assert main(["--config", _sudo_cfg(tmp_path)]) == 3
    err = capsys.readouterr().err
    assert ISOLATION_FAILED in err and "needs Linux" in err
    assert "SECRETVALUE" not in err and "Traceback" not in err


def test_check_os_user_real_launcher_failure_exits_3_without_sweep(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(ou.SystemHostProbe, "platform", lambda self: "win32")

    async def no_sweep(self):
        raise AssertionError("--check-os-user must never sweep")

    monkeypatch.setattr(SudoLauncher, "sweep", no_sweep)
    assert main(["--config", _sudo_cfg(tmp_path), "--check-os-user"]) == 3
    assert "needs Linux" in capsys.readouterr().err


async def test_once_never_starts_launcher(make_config, tmp_path):
    from app.redaction import SecretRedactor
    from app.runtime.wiring import build_runtime, close_runtime

    cfg = make_config(
        opencode={"os_user": {"mode": "sudo"}, "command": [EXE], "managed_server": False},
        database={"path": str(tmp_path / "o.db")},
    )
    rt = build_runtime(cfg, redactor=SecretRedactor(), environ={}, once=True)
    try:
        assert isinstance(rt.launcher, SudoLauncher)
        assert rt.controller._services == []
    finally:
        await close_runtime(rt)


# ---- mode none warning and default ------------------------------------------------------


def test_default_mode_is_sudo_and_build_launcher_default_is_sudo():
    from app.config.models import OsUserConfig

    assert OsUserConfig().mode == "sudo"
    assert OpenCodeConfig().os_user.mode == "sudo"
