"""F1 end to end: the real executor against a mocked /config; the orchestrator fails closed."""

from __future__ import annotations

import asyncio
import contextlib
import sys
from datetime import timedelta
from pathlib import Path

import httpx
import pytest
from conftest import NOW, SECRET, _base_config_dict, secret_env
from oc_helpers import GATEWAY_URL, good_config

from app.bot import format as fmt
from app.config.loader import parse_config
from app.db.attempts import FallbackAttemptRepository
from app.db.tasks import TaskRepository
from app.opencode.config_check import check_server_config, opencode_model_ids
from app.opencode.endpoint import ServerEndpoint
from app.opencode.executor import OpenCodeExecutor
from app.orchestrator.orchestrator import Orchestrator
from app.orchestrator.state_machine import TaskStatus
from app.providers.base import ModelCapability
from app.providers.fake import FakeProviderAdapter
from app.redaction import SecretRedactor
from app.runtime.daemon import AgentController, ControllerEvent, TaskSubmission
from app.runtime.interfaces import SmallModelCheck

FAKE = str(Path(__file__).parent / "fakes" / "fake_opencode.py")


def _cfg():
    return parse_config(_base_config_dict())


CFG = _cfg()
ALLOWED = opencode_model_ids(CFG)
SAFE = sorted(ALLOWED)[0]


def body_with_small(value):
    body = good_config(CFG)
    if value is None:
        body.pop("small_model")
    else:
        body["small_model"] = value
    return body


async def probe(handler, allowed=ALLOWED):
    return await check_server_config(
        ServerEndpoint("http://127.0.0.1:4096"),
        allowed,
        gateway_base_url=GATEWAY_URL,
        timeout_s=1,
        transport=httpx.MockTransport(handler),
    )


# ---- check_server_config small_model lookalikes -----------------------------------------


@pytest.mark.parametrize(
    "value",
    [
        "",
        " ",
        "BROKE/groq/q-x",
        "broke/groq/Q-X",
        " broke/groq/q-x",
        "broke/groq/q-x\n",
        "broke/groq/q-x\t",
        "broke/groq/q-x2",
        "broke/groq/q",
        "broke/groq/q-x/extra",
        "groq/q-x",
        "google/q-x",
        "broke/groq/openrouter/free",
        "broke/openrouter/free",
        "broke/openrouter/openrouter/free:paid",
        "broke/openrouter/openrouter/FREE",
        "broke/openrouter/anthropic/claude-3.5-sonnet",
        "openrouter/openai/gpt-4o",
        ["broke/groq/q-x"],
        {"a": "broke/groq/q-x"},
        True,
        0,
    ],
)
async def test_unsafe_lookalikes_and_paid_ids(value):
    out, _ = await probe(lambda r: httpx.Response(200, json=body_with_small(value)))
    assert out == SmallModelCheck.UNSAFE


@pytest.mark.parametrize("value", sorted(ALLOWED))
async def test_each_allowed_value_ok(value):
    out, _ = await probe(lambda r: httpx.Response(200, json=body_with_small(value)))
    assert out == SmallModelCheck.OK


async def test_empty_allowlist_is_always_unsafe():
    out, _ = await probe(lambda r: httpx.Response(200, json=body_with_small(SAFE)), frozenset())
    assert out == SmallModelCheck.UNSAFE


def test_allowed_models_match_every_enabled_candidate(make_config):
    cfg = make_config()
    expected = {
        f"broke/{p.provider}/{m}"
        for p in cfg.routing.policies.values()
        if p.enabled
        for m in p.model_order
    }
    assert expected and opencode_model_ids(cfg) == expected


# ---- Discord text ----------------------------------------------------------------------


def test_unsafe_config_text_guides_user_and_leaks_nothing():
    out = fmt.format_event(
        ControllerEvent(
            "task_finished",
            at=NOW,
            task_id="abcdef1234",
            fields={
                "status": "FAILED",
                "error_class": "OPENCODE_UNSAFE_CONFIG",
                "output_tail": f"leak {SECRET}",
            },
        ),
        redactor=SecretRedactor([SECRET]),
    )
    assert out is not None
    assert "gateway" in out and "auth.json" in out
    assert "not reachable" not in out
    assert SECRET not in out


# ---- orchestrator + real executor ------------------------------------------------------


class Clock:
    def __init__(self):
        self.now = NOW

    def __call__(self):
        return self.now


class Harness:
    def __init__(self, tmp_path, make_config, config_response, ttl=None):
        orch_cfg = {} if ttl is None else {"orchestrator": {"small_model_check_ttl_s": ttl}}
        self.cfg = make_config(
            database={"path": str(tmp_path / "o.db")},
            opencode={"working_directory": str(tmp_path), "command": [sys.executable, FAKE]},
            routing={"max_fallback_attempts": 5},
            **orch_cfg,
        )
        self.argv = tmp_path / "argv.txt"
        self.config_response = config_response
        self.config_calls = 0
        self.clock = Clock()

        def handler(req):
            if req.url.path == "/config":
                self.config_calls += 1
                return self.config_response(self)
            return httpx.Response(200, json={"healthy": True})

        self.executor = OpenCodeExecutor(
            self.cfg.opencode,
            transport=httpx.MockTransport(handler),
            gateway_base_url=lambda: GATEWAY_URL,
            extra_env={
                "FAKE_OC_SCENARIOS": "success",
                "FAKE_OC_ARGV_FILE": str(self.argv),
                "FAKE_OC_COUNTER_FILE": str(tmp_path / "counter.txt"),
            },
        )
        adapters = {
            pol.provider: FakeProviderAdapter(
                pol.provider,
                models=[
                    ModelCapability(
                        pol.provider, m, supports_tool_calling=True, context_window=200_000
                    )
                    for m in pol.model_order
                ],
            )
            for pol in self.cfg.routing.policies.values()
        }
        self.orch = Orchestrator(
            self.cfg,
            executor=self.executor,
            adapters=adapters,
            environ=secret_env(),
            clock=self.clock,
        )

    @property
    def safe(self):
        return SAFE

    @contextlib.asynccontextmanager
    async def started(self):
        c = AgentController(self.cfg, orchestrator=self.orch)
        await c.start()
        try:
            yield c
        finally:
            await c.stop()

    async def run_task(self, c):
        rec = c.enqueue_task(
            TaskSubmission(
                project_id="default",
                prompt="write code",
                discord_user_id="1",
                discord_channel_id="42",
            )
        )
        assert rec is not None
        await asyncio.wait_for(c._task_queue.join(), 30)
        return TaskRepository(c.conn).get(rec.id)

    def spawned(self):
        return self.argv.exists()


def ok_response(h):
    return httpx.Response(200, json=good_config(h.cfg))


def _raise_connect(h):
    raise httpx.ConnectError("down")


@pytest.mark.parametrize(
    "mutate",
    [
        lambda s: None,
        lambda s: "",
        lambda s: s.upper(),
        lambda s: s + " ",
        lambda s: " " + s,
        lambda s: s + "\n",
        lambda s: s + "x",
        lambda s: s[:-1],
        lambda s: s.split("/", 1)[1],  # drops the gateway prefix
        lambda s: "wrongprov/" + s.split("/", 1)[1],  # wrong provider prefix
        lambda s: "openrouter/anthropic/claude-3.5-sonnet",
        lambda s: "openrouter/openai/gpt-4o",
        lambda s: 7,
        lambda s: [s],
    ],
)
async def test_bad_small_model_fails_closed_without_spawn(tmp_path, make_config, mutate):
    def resp(h):
        return httpx.Response(200, json=body_with_small(mutate(h.safe)))

    h = Harness(tmp_path, make_config, resp)
    async with h.started() as c:
        t = await h.run_task(c)
        assert t.status == TaskStatus.FAILED and t.error_class == "OPENCODE_UNSAFE_CONFIG"
        assert not h.spawned()
        assert FallbackAttemptRepository(c.conn).list_for_task(t.id) == []
        assert h.orch.cooldowns.active(NOW) == {}
        assert t.selected_provider is None and t.selected_model is None


@pytest.mark.parametrize(
    "response",
    [
        lambda h: httpx.Response(500),
        lambda h: httpx.Response(404, json=good_config(h.cfg)),
        lambda h: httpx.Response(200, content=b"not json"),
        lambda h: httpx.Response(200, json=["x"]),
        _raise_connect,
    ],
)
async def test_config_failure_is_unavailable_without_spawn(tmp_path, make_config, response):
    h = Harness(tmp_path, make_config, response)
    async with h.started() as c:
        t = await h.run_task(c)
        assert t.status == TaskStatus.FAILED and t.error_class == "OPENCODE_UNAVAILABLE"
        assert not h.spawned()
        assert FallbackAttemptRepository(c.conn).list_for_task(t.id) == []
        assert h.orch.cooldowns.active(NOW) == {}


async def test_safe_small_model_runs_the_task(tmp_path, make_config):
    h = Harness(tmp_path, make_config, ok_response)
    async with h.started() as c:
        t = await h.run_task(c)
        assert t.status == TaskStatus.SUCCEEDED and h.spawned()


async def test_ttl_cache_honored_then_rechecked(tmp_path, make_config):
    h = Harness(tmp_path, make_config, ok_response, ttl=30)
    async with h.started() as c:
        assert (await h.run_task(c)).status == TaskStatus.SUCCEEDED
        assert (await h.run_task(c)).status == TaskStatus.SUCCEEDED
        assert h.config_calls == 1
        h.clock.now = NOW + timedelta(seconds=31)
        assert (await h.run_task(c)).status == TaskStatus.SUCCEEDED
        assert h.config_calls == 2


async def test_ttl_zero_checks_every_task(tmp_path, make_config):
    h = Harness(tmp_path, make_config, ok_response, ttl=0)
    async with h.started() as c:
        for _ in range(3):
            assert (await h.run_task(c)).status == TaskStatus.SUCCEEDED
        assert h.config_calls == 3


async def test_cached_ok_expires_then_unsafe_config_fails(tmp_path, make_config):
    state = {"bad": False}

    def resp(h):
        if state["bad"]:
            return httpx.Response(200, json=body_with_small("openrouter/openai/gpt-4o"))
        return ok_response(h)

    h = Harness(tmp_path, make_config, resp, ttl=30)
    async with h.started() as c:
        assert (await h.run_task(c)).status == TaskStatus.SUCCEEDED
        state["bad"] = True
        assert (await h.run_task(c)).status == TaskStatus.SUCCEEDED  # within TTL
        h.clock.now = NOW + timedelta(seconds=31)
        t = await h.run_task(c)
        assert t.status == TaskStatus.FAILED and t.error_class == "OPENCODE_UNSAFE_CONFIG"


async def test_failures_are_never_cached(tmp_path, make_config):
    state = {"bad": True}

    def resp(h):
        if state["bad"]:
            return httpx.Response(200, json={})
        return ok_response(h)

    h = Harness(tmp_path, make_config, resp, ttl=3600)
    async with h.started() as c:
        assert (await h.run_task(c)).error_class == "OPENCODE_UNSAFE_CONFIG"
        assert (await h.run_task(c)).error_class == "OPENCODE_UNSAFE_CONFIG"
        assert h.config_calls == 2
        state["bad"] = False
        assert (await h.run_task(c)).status == TaskStatus.SUCCEEDED
