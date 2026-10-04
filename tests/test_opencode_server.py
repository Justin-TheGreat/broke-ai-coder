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
    for m in managers:
        await m.stop()


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
            tmp_path, FAKE_OC_SERVE_SCENARIO="crash_after", FAKE_OC_SERVE_CRASH_AFTER_S="0.5"
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
    assert "groq, openai" in msg and "opencode auth logout" in msg
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
