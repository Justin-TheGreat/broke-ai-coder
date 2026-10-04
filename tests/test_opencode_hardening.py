# ruff: noqa: F811
"""Tester additions: managed-server env/tree-kill, attach env, end-to-end preflight."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import httpx
import pytest
from oc_helpers import good_config
from test_opencode_server import (  # noqa: F401  (started_manager is a fixture)
    FAKE,
    PASSTHROUGH,
    make_environ,
    started_manager,
    wait_for,
)
from test_small_model_preflight import Harness, ok_response  # noqa: F401

from app.config.models import OpenCodeConfig
from app.db.attempts import FallbackAttemptRepository
from app.opencode.executor import OpenCodeExecutor
from app.orchestrator.state_machine import TaskStatus

TREE = str(Path(__file__).parent / "fakes" / "fake_serve_tree.py")


@pytest.fixture(autouse=True)
def _no_real_network() -> None:
    """Loopback only (fake serve); overrides the conftest guard."""
    return None


def _alive(pid: int) -> bool:
    if sys.platform == "win32":
        out = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True, text=True
        ).stdout
        return str(pid) in out
    import os

    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


# ---- managed server --------------------------------------------------------------------------


async def test_parent_opencode_config_content_never_reaches_server(started_manager, tmp_path):
    env = make_environ(
        tmp_path,
        OPENCODE_CONFIG_CONTENT='{"enabled_providers":["openai"],"model":"openai/gpt-4o"}',
        OPENCODE_CONFIG_DIR="/evil",
        OPENCODE_SERVER_PASSWORD="parent-password",
        FOO_API_KEY="foo-REAL",
    )
    m = await started_manager(environ=env)
    served = json.loads((tmp_path / "env.json").read_text())
    inline = json.loads(served["OPENCODE_CONFIG_CONTENT"])
    assert inline["enabled_providers"] == ["broke"] and set(inline["provider"]) == {"broke"}
    assert "openai/gpt-4o" not in served["OPENCODE_CONFIG_CONTENT"]
    assert served["OPENCODE_SERVER_PASSWORD"] == m.password != "parent-password"
    assert "OPENCODE_CONFIG_DIR" not in served and "FOO_API_KEY" not in served
    assert not any("REAL" in v for v in served.values())
    # the real provider-key values must not be in the generated config either
    for v in ("sk-or-REAL-KEY-1", "gem-REAL-KEY-2", "gsk-REAL-KEY-3", "discord-REAL-TOKEN"):
        assert v not in served["OPENCODE_CONFIG_CONTENT"]


async def test_serve_binds_loopback_argv(started_manager, tmp_path):
    m = await started_manager()
    assert m.endpoint.url.startswith("http://127.0.0.1:")
    assert m.endpoint.password == m.password and len(m.password) >= 16
    assert "pw-server" not in repr(m.endpoint)  # repr hides the password


async def test_stop_kills_whole_process_tree(started_manager, tmp_path):
    pidfile = tmp_path / "grandchild.pid"
    env = make_environ(tmp_path, FAKE_OC_GRANDCHILD_PID_FILE=str(pidfile))
    m = await started_manager(
        environ=env,
        command=[sys.executable, TREE],
        env_passthrough=[*PASSTHROUGH, "FAKE_OC_GRANDCHILD_PID_FILE"],
    )
    await wait_for(pidfile.exists)
    pid = int(pidfile.read_text())
    assert _alive(pid)
    await m.stop()
    await wait_for(lambda: not _alive(pid), timeout=15)


async def test_stored_credentials_added_later_block_restart_path_not_leak(
    started_manager, tmp_path
):
    # start fails closed when a bad auth.json appears before the next start()
    m = await started_manager()
    await m.stop()
    store = tmp_path / "xdg" / "opencode"
    store.mkdir(parents=True, exist_ok=True)
    (store / "auth.json").write_text(json.dumps({"openrouter": {"key": "sk-LEAK-1"}}))
    from app.runtime.interfaces import StartupError

    with pytest.raises(StartupError) as ei:
        await m.start()
    assert "openrouter" in str(ei.value) and "LEAK" not in str(ei.value)


# ---- run --attach child env --------------------------------------------------------------------


@pytest.mark.parametrize(
    "var",
    [
        "OPENCODE_CONFIG_CONTENT",
        "OPENCODE_CONFIG",
        "OPENCODE_CONFIG_DIR",
        "OPENROUTER_API_KEY",
        "GEMINI_API_KEY",
        "GOOGLE_GENERATIVE_AI_API_KEY",
    ],
)
async def test_attach_child_has_no_parent_config_or_provider_keys(tmp_path, monkeypatch, var):
    from test_opencode_executor import run

    monkeypatch.setenv(var, "parent-value")
    ex = OpenCodeExecutor(
        OpenCodeConfig(command=[sys.executable, FAKE]),
        extra_env={"FAKE_OC_SCENARIOS": "env_dump", "FAKE_OC_BOT_VAR": var},
    )
    assert (await run(ex, tmp_path)).output_tail.strip() == "present=False"


# ---- end-to-end preflight (orchestrator + real executor, no spawn) ------------------------------


async def test_stored_credentials_fail_every_task_without_spawn(tmp_path, make_config):
    store = tmp_path / "xdg-data" / "opencode"  # conftest points XDG_DATA_HOME here
    store.mkdir(parents=True)
    (store / "auth.json").write_text(json.dumps({"groq": {"key": "sk-LEAK-2"}}))
    h = Harness(tmp_path, make_config, ok_response, ttl=3600)
    async with h.started() as c:
        for _ in range(2):
            t = await h.run_task(c)
            assert t.status == TaskStatus.FAILED and t.error_class == "OPENCODE_UNSAFE_CONFIG"
            assert not h.spawned()
            assert FallbackAttemptRepository(c.conn).list_for_task(t.id) == []
            assert "LEAK" not in repr(t)
        assert h.config_calls == 0  # blocked before any HTTP to the server


def _extra_provider(b):
    b["provider"]["openai"] = {"options": {"apiKey": "k"}}


def _other_enabled(b):
    b["enabled_providers"] = ["broke", "openai"]


def _other_baseurl(b):
    b["provider"]["broke"]["options"]["baseURL"] = "https://api.openai.com/v1"


def _paid_model_key(b):
    b["provider"]["broke"]["models"]["openai/gpt-4o"] = {}


def _no_enabled(b):
    b.pop("enabled_providers")


@pytest.mark.parametrize(
    "mutate", [_extra_provider, _other_enabled, _other_baseurl, _paid_model_key, _no_enabled]
)
async def test_server_config_mismatch_fails_closed_without_spawn(tmp_path, make_config, mutate):
    def resp(h):
        body = good_config(h.cfg)
        mutate(body)
        return httpx.Response(200, json=body)

    h = Harness(tmp_path, make_config, resp)
    async with h.started() as c:
        t = await h.run_task(c)
        assert t.status == TaskStatus.FAILED and t.error_class == "OPENCODE_UNSAFE_CONFIG"
        assert not h.spawned()
        assert FallbackAttemptRepository(c.conn).list_for_task(t.id) == []
