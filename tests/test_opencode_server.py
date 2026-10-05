"""Managed `opencode serve`: inline config content and lifecycle against the fake serve script.

The lifecycle tests talk to a fake server on 127.0.0.1 only, so they opt out of the autouse
"no real network" guard that blocks every httpx transport.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import os
import socket
import sys
import time
from pathlib import Path

import httpx
import pytest
from oc_helpers import GATEWAY_TOKEN, GATEWAY_URL

import app.opencode.server as server_mod
from app.config.models import AppConfig
from app.opencode.os_user import DirectLauncher
from app.opencode.server import OpenCodeServerManager, build_opencode_config
from app.redaction import SecretRedactor
from app.runtime.interfaces import StartupError

FAKE = str(Path(__file__).parent / "fakes" / "fake_opencode.py")
PASSTHROUGH = [
    "FAKE_OC_SERVE_SCENARIO",
    "FAKE_OC_ENV_FILE",
    "FAKE_OC_SERVE_COUNTER_FILE",
    "FAKE_OC_SERVE_CRASH_AFTER_S",
]


@pytest.fixture(autouse=True)
def _no_real_network() -> None:
    """Override the conftest guard: these tests only use loopback."""
    return None


class StubGateway:
    base_url = GATEWAY_URL
    token = GATEWAY_TOKEN


# ---- build_opencode_config ------------------------------------------------------------


def test_config_content_shape(make_config):
    cfg = build_opencode_config(
        make_config(), gateway_base_url=GATEWAY_URL, gateway_token=GATEWAY_TOKEN
    )
    assert cfg["enabled_providers"] == ["broke"]
    assert set(cfg["provider"]) == {"broke"}
    broke = cfg["provider"]["broke"]
    assert broke["npm"] == "@ai-sdk/openai-compatible"
    assert broke["options"] == {"baseURL": GATEWAY_URL, "apiKey": GATEWAY_TOKEN}
    assert list(broke["models"]) == [
        "openrouter/openrouter/free",
        "gemini/g-3.8",
        "gemini/g-3.7",
        "groq/q-x",
        "groq/q-y",
    ]
    assert cfg["model"] == "broke/openrouter/openrouter/free"
    assert cfg["small_model"] == "broke/openrouter/openrouter/free"
    assert cfg["share"] == "disabled" and cfg["autoupdate"] is False
    assert cfg["$schema"] == "https://opencode.ai/config.json"
    json.dumps(cfg)  # serializable


def test_config_small_model_override(make_config):
    cfg = build_opencode_config(
        make_config(gateway={"small_model": "groq/q-y"}),
        gateway_base_url=GATEWAY_URL,
        gateway_token=GATEWAY_TOKEN,
    )
    assert cfg["small_model"] == "broke/groq/q-y"
    assert cfg["model"] == "broke/openrouter/openrouter/free"


def test_config_limit_included_only_when_both_known(make_config):
    cfg = make_config(
        providers={
            "gemini": {
                "enabled": True,
                "api_key_env": "GEMINI_API_KEY",
                "models": {
                    "g-3.8": {"context_window": 1000, "max_output_tokens": 200},
                    "g-3.7": {"context_window": 1000},
                },
            }
        }
    )
    models = build_opencode_config(cfg, gateway_base_url=GATEWAY_URL, gateway_token="t")[
        "provider"
    ]["broke"]["models"]
    assert models["gemini/g-3.8"] == {
        "name": "gemini/g-3.8",
        "limit": {"context": 1000, "output": 200},
    }
    assert models["gemini/g-3.7"] == {"name": "gemini/g-3.7"}
    assert models["groq/q-x"] == {"name": "groq/q-x"}


def test_config_has_no_real_keys_or_paid_models(make_config):
    text = json.dumps(
        build_opencode_config(
            make_config(), gateway_base_url=GATEWAY_URL, gateway_token=GATEWAY_TOKEN
        )
    )
    assert "API_KEY" not in text and "sk-" not in text


def test_config_without_models_raises_startup_error(make_config):
    cfg = make_config(routing={"provider_order": []})
    with pytest.raises(StartupError, match="no enabled allowlisted models"):
        build_opencode_config(cfg, gateway_base_url=GATEWAY_URL, gateway_token="t")


# ---- lifecycle ------------------------------------------------------------------------


def lifecycle_config(make_config, tmp_path, **opencode) -> AppConfig:
    return make_config(
        opencode={
            "command": [sys.executable, FAKE],
            "server_port": 0,
            "server_directory": str(tmp_path / "serve-dir"),
            "server_start_timeout_s": 15,
            "server_restart_max_backoff_s": 1,
            "kill_grace_s": 1,
            "env_passthrough": PASSTHROUGH,
            **opencode,
        }
    )


def make_environ(tmp_path, **extra) -> dict[str, str]:
    return {
        **os.environ,
        "OPENROUTER_API_KEY": "sk-or-REAL-KEY-1",
        "GEMINI_API_KEY": "gem-REAL-KEY-2",
        "GROQ_API_KEY": "gsk-REAL-KEY-3",
        "GOOGLE_GENERATIVE_AI_API_KEY": "gg-REAL-KEY-4",
        "DISCORD_BOT_TOKEN": "discord-REAL-TOKEN",
        "OPENCODE_CONFIG": "/evil.json",
        "XDG_DATA_HOME": str(tmp_path / "xdg"),
        "FAKE_OC_ENV_FILE": str(tmp_path / "env.json"),
        "FAKE_OC_SERVE_COUNTER_FILE": str(tmp_path / "serve-count.txt"),
        **extra,
    }


def serve_count(tmp_path) -> int:
    p = tmp_path / "serve-count.txt"
    return int(p.read_text()) if p.exists() else 0


async def get(endpoint, path, *, auth=True):
    async with httpx.AsyncClient(trust_env=False, timeout=3) as client:
        return await client.get(endpoint.url + path, auth=endpoint.auth() if auth else None)


async def wait_for(pred, timeout=20.0):
    end = time.monotonic() + timeout
    while True:
        value = await pred() if inspect.iscoroutinefunction(pred) else pred()
        if value:
            return value
        assert time.monotonic() < end, "timed out waiting"
        await asyncio.sleep(0.1)


@pytest.fixture
async def started_manager(make_config, tmp_path):
    managers: list[OpenCodeServerManager] = []

    async def _start(*, environ=None, redactor=None, **opencode) -> OpenCodeServerManager:
        cfg = lifecycle_config(make_config, tmp_path, **opencode)
        m = OpenCodeServerManager(
            cfg,
            gateway=StubGateway(),  # type: ignore[arg-type]
            redactor=redactor,
            environ=environ if environ is not None else make_environ(tmp_path),
            password="pw-server-0123456789",
        )
        managers.append(m)
        await m.start()
        return m

    yield _start
    errors: list[BaseException] = []
    for m in managers:
        try:
            await m.stop()
        except Exception as e:  # keep stopping the rest; report afterwards
            errors.append(e)
    if errors:
        raise errors[0]


async def test_starts_healthy_with_key_free_env_and_inline_config(started_manager, tmp_path):
    m = await started_manager()
    assert m.endpoint.url.startswith("http://127.0.0.1:")
    assert (tmp_path / "serve-dir").is_dir()
    # health requires the Basic-auth password
    assert (await get(m.endpoint, "/global/health", auth=False)).status_code == 401
    assert (await get(m.endpoint, "/global/health")).status_code == 200
    env = json.loads((tmp_path / "env.json").read_text())
    for name in (
        "OPENROUTER_API_KEY",
        "GEMINI_API_KEY",
        "GROQ_API_KEY",
        "GOOGLE_GENERATIVE_AI_API_KEY",
        "DISCORD_BOT_TOKEN",
        "OPENCODE_CONFIG",
    ):
        assert name not in env, name
    assert not any("REAL-" in v for v in env.values())
    assert env["OPENCODE_SERVER_PASSWORD"] == "pw-server-0123456789"
    assert env["OPENCODE_SERVER_USERNAME"] == "opencode"
    inline = json.loads(env["OPENCODE_CONFIG_CONTENT"])
    assert inline["enabled_providers"] == ["broke"]
    assert inline["provider"]["broke"]["options"]["apiKey"] == GATEWAY_TOKEN
    # the fake serves GET /config from the inline content
    body = (await get(m.endpoint, "/config")).json()
    assert body == inline


async def test_serves_on_configured_fixed_port(started_manager):
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    m = await started_manager(server_port=port)
    assert m.endpoint.url == f"http://127.0.0.1:{port}"
    assert (await get(m.endpoint, "/global/health")).status_code == 200


async def test_crash_triggers_restart(started_manager, tmp_path, monkeypatch):
    monkeypatch.setattr(server_mod, "_INITIAL_BACKOFF_S", 0.1)
    monkeypatch.setattr(server_mod, "_BACKOFF_RESET_S", 0.5)
    m = await started_manager(
        environ=make_environ(
            tmp_path, FAKE_OC_SERVE_SCENARIO="crash_after", FAKE_OC_SERVE_CRASH_AFTER_S="1.5"
        )
    )
    assert serve_count(tmp_path) == 1
    await wait_for(lambda: serve_count(tmp_path) == 2)

    async def healthy():
        try:
            return (await get(m.endpoint, "/global/health")).status_code == 200
        except httpx.HTTPError:
            return False

    await wait_for(healthy)
    assert serve_count(tmp_path) == 2  # the second run does not crash again


async def test_stop_kills_server_and_is_idempotent(started_manager):
    m = await started_manager()
    endpoint = m.endpoint
    assert (await get(endpoint, "/global/health")).status_code == 200
    await m.stop()
    await m.stop()
    with pytest.raises(httpx.HTTPError):
        await get(endpoint, "/global/health")


async def test_never_healthy_raises_startup_error_within_timeout(started_manager, tmp_path):
    start = time.monotonic()
    with pytest.raises(StartupError, match="did not become healthy"):
        await started_manager(
            environ=make_environ(tmp_path, FAKE_OC_SERVE_SCENARIO="never_healthy"),
            server_start_timeout_s=1.5,
        )
    assert time.monotonic() - start < 10


async def test_early_exit_raises_with_redacted_stderr_detail(started_manager, tmp_path):
    with pytest.raises(StartupError) as ei:
        await started_manager(
            environ=make_environ(tmp_path, FAKE_OC_SERVE_SCENARIO="exit_immediately")
        )
    msg = str(ei.value)
    assert msg.startswith("opencode serve did not become healthy: ")
    assert "exit code 5" in msg and "boom" in msg
    assert len(msg) < 400


async def test_stderr_detail_is_redacted(started_manager, tmp_path):
    # "boom" is the fake's stderr text; register it as a secret to prove redaction applies.
    with pytest.raises(StartupError) as ei:
        await started_manager(
            environ=make_environ(tmp_path, FAKE_OC_SERVE_SCENARIO="exit_immediately"),
            redactor=SecretRedactor(["boom"], min_length=4),
        )
    assert "boom" not in str(ei.value) and "[REDACTED]" in str(ei.value)


async def test_stored_credentials_block_startup_naming_ids_only(started_manager, tmp_path):
    store = tmp_path / "xdg" / "opencode"
    store.mkdir(parents=True)
    (store / "auth.json").write_text(
        json.dumps({"openai": {"key": "sk-VERY-SECRET"}, "groq": {"key": "gsk-SECRET2"}})
    )
    with pytest.raises(StartupError) as ei:
        await started_manager()
    msg = str(ei.value)
    assert "groq, openai" in msg and "rm -f" in msg
    assert "SECRET" not in msg
    assert serve_count(tmp_path) == 0  # never spawned


async def test_missing_executable_raises_startup_error(started_manager, tmp_path):
    with pytest.raises(StartupError, match="opencode executable not found"):
        await started_manager(command=["no-such-opencode-binary-xyz"])


async def test_empty_allowlist_raises_startup_error(make_config, tmp_path):
    cfg = lifecycle_config(make_config, tmp_path)
    cfg = cfg.model_copy(update={"routing": cfg.routing.model_copy(update={"provider_order": []})})
    m = OpenCodeServerManager(
        cfg,
        gateway=StubGateway(),
        environ=make_environ(tmp_path),  # type: ignore[arg-type]
    )
    with pytest.raises(StartupError, match="no enabled allowlisted models"):
        await m.start()
    await m.stop()
    assert serve_count(tmp_path) == 0


# ---- foreign servers / port ownership --------------------------------------------------


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


async def start_foreign(port: int, *, status_unauth: int = 200):
    """A plain server answering /global/health; status_unauth is returned without credentials."""
    hits: list[bytes] = []

    async def handle(reader, writer):
        data = await reader.read(4096)
        hits.append(data)
        has_auth = b"authorization:" in data.lower()
        status = 200 if (has_auth or status_unauth == 200) else status_unauth
        body = b'{"healthy": true, "version": "x"}' if status == 200 else b"{}"
        head = (
            f"HTTP/1.1 {status} X\r\nContent-Type: application/json\r\n"
            f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n"
        )
        writer.write(head.encode() + body)
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", port)
    return server, hits


async def test_occupied_fixed_port_fails_startup_and_never_spawns(started_manager, tmp_path):
    port = free_port()
    server, hits = await start_foreign(port)
    try:
        with pytest.raises(StartupError, match="already in use"):
            await started_manager(server_port=port)
    finally:
        server.close()
        await server.wait_closed()
    assert serve_count(tmp_path) == 0  # our serve was never spawned
    assert hits == []  # and nothing talked to the foreign server


async def test_foreign_server_without_401_is_rejected_even_if_port_check_is_bypassed(
    started_manager, tmp_path, monkeypatch
):
    """Defence in depth: a healthy server that ignores the password is not accepted."""
    port = free_port()
    server, _ = await start_foreign(port, status_unauth=200)
    monkeypatch.setattr(server_mod, "_port_in_use", lambda p: False)
    try:
        with pytest.raises(StartupError, match="does not enforce the password"):
            await started_manager(server_port=port, server_start_timeout_s=5)
    finally:
        server.close()
        await server.wait_closed()


async def test_foreign_server_with_other_unauth_status_is_rejected(
    started_manager, tmp_path, monkeypatch
):
    port = free_port()
    server, _ = await start_foreign(port, status_unauth=403)
    monkeypatch.setattr(server_mod, "_port_in_use", lambda p: False)
    try:
        with pytest.raises(StartupError, match="did not become healthy"):
            await started_manager(
                environ=make_environ(tmp_path, FAKE_OC_SERVE_SCENARIO="exit_immediately"),
                server_port=port,
                server_start_timeout_s=3,
            )
    finally:
        server.close()
        await server.wait_closed()


async def test_restart_with_port_taken_gives_up_with_clear_error(
    started_manager, tmp_path, monkeypatch
):
    monkeypatch.setattr(server_mod, "_INITIAL_BACKOFF_S", 0.1)
    port = free_port()
    m = await started_manager(
        environ=make_environ(
            tmp_path, FAKE_OC_SERVE_SCENARIO="crash_after", FAKE_OC_SERVE_CRASH_AFTER_S="1.5"
        ),
        server_port=port,
    )
    await wait_for(lambda: m._proc is None or m._proc.returncode is not None)
    server = None
    while server is None:  # grab the port as soon as our crashed serve releases it
        try:
            server, _ = await start_foreign(port)
        except OSError:
            await asyncio.sleep(0.05)
    try:
        await wait_for(lambda: m.fatal_error is not None)
        assert "already in use" in m.fatal_error
        await asyncio.wait_for(m._monitor, 5)  # the monitor stopped retrying
        assert serve_count(tmp_path) == 1  # no further spawn attempts
    finally:
        server.close()
        await server.wait_closed()


async def test_launch_listener_called_on_every_launch(make_config, tmp_path, monkeypatch):
    monkeypatch.setattr(server_mod, "_INITIAL_BACKOFF_S", 0.1)
    monkeypatch.setattr(server_mod, "_BACKOFF_RESET_S", 0.5)
    calls: list[int] = []
    m = OpenCodeServerManager(
        lifecycle_config(make_config, tmp_path),
        gateway=StubGateway(),  # type: ignore[arg-type]
        environ=make_environ(
            tmp_path, FAKE_OC_SERVE_SCENARIO="crash_after", FAKE_OC_SERVE_CRASH_AFTER_S="1.5"
        ),
        password="pw-server-0123456789",
    )
    m.add_launch_listener(lambda: calls.append(1))
    await m.start()
    try:
        assert len(calls) == 1
        await wait_for(lambda: len(calls) >= 2)  # relaunch after the crash notifies again
    finally:
        await m.stop()


# ---- readiness: crash-to-relaunch window and fatal error ---------------------------------


def make_executor(m: OpenCodeServerManager):
    from app.opencode.executor import OpenCodeExecutor

    return OpenCodeExecutor(
        m._oc, endpoint=lambda: m.endpoint, unavailable_reason=m.unavailable_reason
    )


async def grab_port(port: int, timeout: float = 10.0):
    """Bind a foreign server as soon as our crashed serve releases the port.

    Fails (instead of hanging) if the port never frees up, e.g. if the manager's
    relaunch won the race and now holds it.
    """
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        try:
            return await start_foreign(port)
        except OSError:
            if asyncio.get_running_loop().time() >= deadline:
                raise AssertionError(f"port {port} never freed for the foreign server") from None
            await asyncio.sleep(0.05)


async def test_crash_window_clears_cache_and_health_is_unhealthy(
    make_config, tmp_path, monkeypatch
):
    monkeypatch.setattr(server_mod, "_INITIAL_BACKOFF_S", 30.0)  # stay in the window
    port = free_port()
    cache_valid = [True]
    m = OpenCodeServerManager(
        lifecycle_config(make_config, tmp_path, server_port=port),
        gateway=StubGateway(),  # type: ignore[arg-type]
        environ=make_environ(
            tmp_path, FAKE_OC_SERVE_SCENARIO="crash_after", FAKE_OC_SERVE_CRASH_AFTER_S="1.5"
        ),
        password="pw-server-0123456789",
    )
    m.add_launch_listener(lambda: cache_valid.__setitem__(0, False))
    executor = make_executor(m)
    await m.start()
    server = None
    try:
        assert m.ready and m.unavailable_reason() is None
        cache_valid[0] = True  # the orchestrator cached a server-config OK at launch
        assert (await executor.health_check()).healthy
        await wait_for(lambda: not m.ready)  # the monitor saw the crash
        assert cache_valid[0] is False  # invalidated at the crash, before any relaunch
        server, hits = await grab_port(port)
        health = await executor.health_check()
        assert not health.healthy and "managed OpenCode server is down" in health.detail
        assert hits == []  # the foreign server on the port was never contacted
        assert serve_count(tmp_path) == 1  # nothing spawned (still in backoff)
    finally:
        await m.stop()
        if server is not None:
            server.close()
            await server.wait_closed()
    assert not m.ready


async def test_fatal_error_keeps_health_unhealthy_with_reason(make_config, tmp_path, monkeypatch):
    # Backoff long enough that the foreign server always binds before the relaunch,
    # even under full-suite load; the relaunch then finds the port taken -> fatal_error.
    monkeypatch.setattr(server_mod, "_INITIAL_BACKOFF_S", 3.0)
    port = free_port()
    m = OpenCodeServerManager(
        lifecycle_config(make_config, tmp_path, server_port=port),
        gateway=StubGateway(),  # type: ignore[arg-type]
        environ=make_environ(
            tmp_path, FAKE_OC_SERVE_SCENARIO="crash_after", FAKE_OC_SERVE_CRASH_AFTER_S="1.5"
        ),
        password="pw-server-0123456789",
    )
    executor = make_executor(m)
    await m.start()
    server = None
    try:
        await wait_for(lambda: not m.ready)
        server, hits = await grab_port(port)
        assert serve_count(tmp_path) == 1  # grabbed before any relaunch
        await wait_for(lambda: m.fatal_error is not None)
        for _ in range(3):
            health = await executor.health_check()
            assert not health.healthy
            assert "managed OpenCode server is down" in health.detail
            assert "already in use" in health.detail
            assert "pw-server" not in health.detail
            await asyncio.sleep(0.1)
        assert not m.ready
        assert hits == [] and serve_count(tmp_path) == 1
    finally:
        await m.stop()
        if server is not None:
            server.close()
            await server.wait_closed()


async def test_tasks_run_again_after_successful_relaunch(make_config, tmp_path, monkeypatch):
    monkeypatch.setattr(server_mod, "_INITIAL_BACKOFF_S", 0.1)
    monkeypatch.setattr(server_mod, "_BACKOFF_RESET_S", 0.5)
    m = OpenCodeServerManager(
        lifecycle_config(make_config, tmp_path),
        gateway=StubGateway(),  # type: ignore[arg-type]
        environ=make_environ(
            tmp_path, FAKE_OC_SERVE_SCENARIO="crash_after", FAKE_OC_SERVE_CRASH_AFTER_S="1.5"
        ),
        password="pw-server-0123456789",
    )
    executor = make_executor(m)
    await m.start()
    try:
        assert (await executor.health_check()).healthy
        await wait_for(lambda: serve_count(tmp_path) == 2)

        async def healthy_again():
            return m.ready and (await executor.health_check()).healthy

        await wait_for(healthy_again)
        assert m.fatal_error is None
    finally:
        await m.stop()
    assert not m.ready


# ---- launcher integration (T072) and N10 -----------------------------------------------


class RecordingLauncher(DirectLauncher):
    """Direct behaviour, recording every call, so the fake serve still runs."""

    def __init__(self, *, isolated: bool = False, problem: str | None = None) -> None:
        super().__init__()
        self.isolated = isolated
        self.problem = problem
        self.calls: list[str] = []
        self.commands: list[list[str]] = []
        self.envs: list[dict[str, str]] = []

    def command(self, argv):
        self.commands.append(list(argv))
        return super().command(argv)

    def environment(self, environ, *, passthrough, extra):
        env = super().environment(environ, passthrough=passthrough, extra=extra)
        env["LAUNCHER_MARK"] = "1"
        self.envs.append(env)
        return env

    async def kill_tree(self, proc, *, grace_s):
        self.calls.append("kill_tree")
        await super().kill_tree(proc, grace_s=grace_s)

    async def sweep(self):
        self.calls.append("sweep")

    async def auth_store_problem(self):
        self.calls.append("auth_store_problem")
        return self.problem


def launcher_manager(make_config, tmp_path, launcher, **opencode) -> OpenCodeServerManager:
    return OpenCodeServerManager(
        lifecycle_config(make_config, tmp_path, **opencode),
        gateway=StubGateway(),  # type: ignore[arg-type]
        environ=make_environ(tmp_path),
        password="pw-server-0123456789",
        launcher=launcher,
    )


async def test_serve_argv_and_env_come_from_the_launcher(make_config, tmp_path):
    launcher = RecordingLauncher()
    m = launcher_manager(make_config, tmp_path, launcher)
    await m.start()
    try:
        assert len(launcher.commands) == 1
        argv = launcher.commands[0]
        assert argv[1:] == [FAKE, "serve", "--hostname", "127.0.0.1", "--port", argv[-1]]
        env = launcher.envs[0]
        assert set(env) >= {
            "OPENCODE_CONFIG_CONTENT",
            "OPENCODE_SERVER_PASSWORD",
            "OPENCODE_SERVER_USERNAME",
        }
        assert json.loads((tmp_path / "env.json").read_text())["LAUNCHER_MARK"] == "1"
    finally:
        await m.stop()


async def test_reap_kills_tree_then_sweeps(make_config, tmp_path):
    launcher = RecordingLauncher()
    m = launcher_manager(make_config, tmp_path, launcher)
    await m.start()
    launcher.calls.clear()
    await m.stop()
    assert launcher.calls == ["kill_tree", "sweep"]
    launcher.calls.clear()
    await m.stop()  # idempotent: no proc, but the sweep still runs
    assert launcher.calls == ["sweep"]


async def test_sweep_failure_never_breaks_reap(make_config, tmp_path):
    class Boom(RecordingLauncher):
        async def sweep(self):
            raise RuntimeError("sweep exploded")

    m = launcher_manager(make_config, tmp_path, Boom())
    await m.start()
    await m.stop()
    assert not m.ready


async def test_start_uses_launcher_auth_problem(make_config, tmp_path):
    launcher = RecordingLauncher(problem="OpenCode has stored provider credentials for: x.")
    m = launcher_manager(make_config, tmp_path, launcher)
    with pytest.raises(StartupError, match="stored provider credentials"):
        await m.start()
    assert launcher.commands == [] and serve_count(tmp_path) == 0


@pytest.mark.parametrize("isolated", [False, True])
async def test_password_hint_only_when_isolated(make_config, tmp_path, monkeypatch, isolated):
    port = free_port()
    server, _ = await start_foreign(port, status_unauth=200)
    monkeypatch.setattr(server_mod, "_port_in_use", lambda p: False)
    m = launcher_manager(
        make_config,
        tmp_path,
        RecordingLauncher(isolated=isolated),
        server_port=port,
        server_start_timeout_s=5,
    )
    try:
        with pytest.raises(StartupError) as ei:
            await m.start()
    finally:
        server.close()
        await server.wait_closed()
    msg = str(ei.value)
    assert "does not enforce the password" in msg
    assert ("env_keep line is missing" in msg) is isolated


def test_unavailable_reason_after_fatal_leads_with_restart_instruction(make_config, tmp_path):
    m = launcher_manager(make_config, tmp_path, RecordingLauncher())
    m.fatal_error = (
        "port 4096 already in use - stop any manually started `opencode serve`" + "x" * 400
    )
    reason = m.unavailable_reason()
    assert reason is not None and len(reason) <= 300
    assert reason.startswith("managed OpenCode server is down")
    assert "restart the daemon" in reason
    m.fatal_error = "port 4096 already in use"
    assert "already in use" in (m.unavailable_reason() or "")
    m.fatal_error = None
    assert m.unavailable_reason() == "managed OpenCode server is down: restarting"


# ---- review fixes: S1 (control characters) and S4 (serve session registration) ------------


def _bare_manager(make_config, tmp_path, **kw) -> OpenCodeServerManager:
    return OpenCodeServerManager(
        lifecycle_config(make_config, tmp_path),
        gateway=StubGateway(),  # type: ignore[arg-type]
        environ=make_environ(tmp_path),
        password="pw-server-0123456789",
        **kw,
    )


def test_stderr_detail_strips_terminal_control_sequences(make_config, tmp_path):
    m = _bare_manager(make_config, tmp_path)
    m._stderr = bytearray(b"bad\x1b]52;c;ZXZpbA==\x07 thing\x1b[2J\x00\xc2\x85 end\r\nnext")
    detail = m._stderr_detail()
    assert detail == "bad thing end  next"
    assert "\x1b" not in m._detail("exit code 1")


def test_stderr_detail_strips_before_redacting(make_config, tmp_path):
    m = _bare_manager(make_config, tmp_path, redactor=SecretRedactor(["hunter22"], min_length=4))
    m._stderr = bytearray(b"hun\x1b[0mter22 leaked")  # split by an escape to dodge redaction
    assert "hunter22" not in m._stderr_detail()
    assert "[REDACTED]" in m._stderr_detail()


async def test_manager_registers_serve_session_with_launcher(make_config, tmp_path):
    from app.opencode.os_user import DirectLauncher

    class Rec(DirectLauncher):
        def __init__(self) -> None:
            super().__init__()
            self.registered: list[int | None] = []

        def register_serve(self, pid):
            self.registered.append(pid)

    rec = Rec()
    m = _bare_manager(make_config, tmp_path, launcher=rec)
    await m.start()
    try:
        assert rec.registered and isinstance(rec.registered[-1], int)
        pid = rec.registered[-1]
    finally:
        await m.stop()
    assert rec.registered[-1] is None and pid > 0


def test_fake_serve_exits_by_itself_after_max_lifetime(tmp_path):
    import subprocess
    import sys

    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    env = {**os.environ, "FAKE_OC_MAX_LIFETIME_S": "1"}
    p = subprocess.Popen([sys.executable, FAKE, "serve", "--port", str(port)], env=env)
    try:
        assert p.wait(timeout=15) == 0
    finally:
        if p.poll() is None:
            p.kill()
            p.wait()
