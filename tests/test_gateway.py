from __future__ import annotations

import json
import logging
from collections.abc import Callable
from datetime import timedelta
from typing import Any

import httpx
import pytest
from aiohttp.test_utils import TestClient, TestServer
from conftest import NOW, SECRET, secret_env

from app.db.usage import UsageEvent, UsageStatus
from app.gateway.server import LlmGateway
from app.providers.base import CostClass
from app.providers.registry import build_adapters
from app.quota.cooldown import CooldownManager
from app.redaction import SecretRedactor

TOKEN = "gw-token-ABCDEFGHIJKLMNOP"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
OR = secret_env()["OPENROUTER_API_KEY"]
GE = secret_env()["GEMINI_API_KEY"]
GQ = secret_env()["GROQ_API_KEY"]

OK_BODY = {
    "id": "x",
    "choices": [{"message": {"role": "assistant", "content": "hi"}}],
    "usage": {"prompt_tokens": 11, "completion_tokens": 7},
}


class Harness:
    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.events: list[UsageEvent] = []
        self.handler: Callable[[httpx.Request], httpx.Response] = lambda r: httpx.Response(
            200, json=OK_BODY
        )
        self.task_id: str | None = "task-1"
        self.gw: LlmGateway
        self.cooldowns: CooldownManager
        self.client: TestClient
        self._server: TestServer

    def _transport(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.handler(request)


@pytest.fixture
async def make_harness(make_config):
    made: list[Harness] = []

    async def _make(environ: dict[str, str] | None = None, **cfg: Any) -> Harness:
        h = Harness()
        config = make_config(**cfg)
        env = secret_env() if environ is None else environ
        transport = httpx.MockTransport(h._transport)
        redactor = SecretRedactor([TOKEN])
        adapters = build_adapters(
            config, transport=transport, environ=env, clock=lambda: NOW, redactor=redactor
        )
        h.cooldowns = CooldownManager(config.cooldown, clock=lambda: NOW)
        h.gw = LlmGateway(
            config,
            adapters=adapters,
            cooldowns=h.cooldowns,
            usage_sink=h.events.append,
            current_task_id=lambda: h.task_id,
            redactor=redactor,
            transport=transport,
            token=TOKEN,
            clock=lambda: NOW,
        )
        h._server = TestServer(h.gw.build_app())
        h.client = TestClient(h._server)
        await h.client.start_server()
        made.append(h)
        return h

    yield _make
    for h in made:
        await h.client.close()
        await h.gw.stop()


def chat(model: str = "groq/q-x", **extra: Any) -> dict[str, Any]:
    return {"model": model, "messages": [{"role": "user", "content": "hello"}], **extra}


async def post(h: Harness, body: Any, headers: dict[str, str] | None = None):
    kw: dict[str, Any] = {"headers": AUTH if headers is None else headers}
    if isinstance(body, bytes):
        kw["data"] = body
    else:
        kw["json"] = body
    return await h.client.post("/v1/chat/completions", **kw)


@pytest.mark.parametrize(
    "headers",
    [{}, {"Authorization": "Bearer wrong"}, {"Authorization": TOKEN}, {"x-api-key": TOKEN}],
)
async def test_auth_required_no_upstream(make_harness, headers):
    h = await make_harness()
    r = await post(h, chat(), headers=headers)
    assert r.status == 401
    assert (await r.json())["error"]["code"] == "invalid_api_key"
    r = await h.client.get("/v1/models", headers=headers)
    assert r.status == 401
    assert h.requests == [] and h.events == []


async def test_auth_checked_before_body(make_harness):
    h = await make_harness()
    r = await post(h, b"{not json", headers={})
    assert r.status == 401


@pytest.mark.parametrize(
    "model",
    [
        "groq/not-listed",
        "q-x",  # upstream id without provider prefix
        "gemini/g-3.8x",
        "openrouter/paid-model",
        "broke/groq/q-x",
        None,
        ["groq/q-x"],
        5,
    ],
)
async def test_model_not_allowlisted(make_harness, model):
    h = await make_harness()
    r = await post(h, {"model": model, "messages": []})
    assert r.status == 403
    assert (await r.json())["error"]["code"] == "model_not_allowlisted"
    assert h.requests == [] and h.events == []


async def test_disabled_policy_and_unordered_policy_models_rejected(make_harness):
    h = await make_harness(
        routing={
            "provider_order": ["groq-free", "gemini-free"],
            "policies": {
                "openrouter-free": {"provider": "openrouter", "model_order": ["openrouter/free"]},
                "gemini-free": {"provider": "gemini", "enabled": False, "model_order": ["g-3.8"]},
                "groq-free": {"provider": "groq", "model_order": ["q-x"]},
                "extra": {"provider": "groq", "model_order": ["q-extra"]},
            },
        }
    )
    assert list(h.gw.models) == ["groq/q-x"]
    for model in ("gemini/g-3.8", "openrouter/openrouter/free", "groq/q-extra"):
        r = await post(h, chat(model))
        assert r.status == 403
    assert h.requests == []
    r = await post(h, chat("groq/q-x"))
    assert r.status == 200


@pytest.mark.parametrize(
    ("gid", "url", "key"),
    [
        ("openrouter/openrouter/free", "https://openrouter.ai/api/v1/chat/completions", OR),
        (
            "gemini/g-3.8",
            "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions",
            GE,
        ),
        ("groq/q-x", "https://api.groq.com/openai/v1/chat/completions", GQ),
    ],
)
async def test_upstream_url_and_real_key(make_harness, gid, url, key):
    h = await make_harness()
    r = await post(
        h,
        chat(gid),
        headers={**AUTH, "X-Api-Key": "client-key", "Cookie": "a=b", "X-Custom": "1"},
    )
    assert r.status == 200
    assert len(h.requests) == 1
    req = h.requests[0]
    assert str(req.url) == url
    assert req.method == "POST"
    assert req.headers["authorization"] == f"Bearer {key}"
    assert TOKEN not in json.dumps(dict(req.headers))
    for name in ("x-api-key", "cookie", "x-custom"):
        assert name not in req.headers
    assert json.loads(req.content)["model"] == gid.split("/", 1)[1]


async def test_body_filtering_and_model_rewrite(make_harness):
    h = await make_harness()
    r = await post(
        h,
        chat(
            "openrouter/openrouter/free",
            temperature=0.2,
            tools=[],
            models=["paid/x"],
            provider={"order": ["x"]},
            plugins=[{"id": "web"}],
            route="fallback",
            transforms=["middle-out"],
        ),
    )
    assert r.status == 200
    sent = json.loads(h.requests[0].content)
    assert sent["model"] == "openrouter/free"
    for dropped in ("models", "provider", "plugins", "route", "transforms"):
        assert dropped not in sent
    assert set(sent) == {"model", "messages", "temperature", "tools"}


async def test_non_stream_usage_and_task_id(make_harness):
    h = await make_harness()
    h.handler = lambda r: httpx.Response(200, json=OK_BODY, headers={"x-request-id": "req-9"})
    r = await post(h, chat())
    assert r.status == 200
    assert await r.json() == OK_BODY
    assert r.headers["Content-Type"].startswith("application/json")
    (ev,) = h.events
    assert (ev.provider, ev.model) == ("groq", "q-x")
    assert ev.status == UsageStatus.SUCCESS
    assert (ev.input_tokens, ev.output_tokens) == (11, 7)
    assert ev.task_id == "task-1" and ev.request_id == "req-9"
    assert ev.cost_class == CostClass.FREE


async def test_non_stream_without_usage_has_null_tokens(make_harness):
    h = await make_harness()
    h.handler = lambda r: httpx.Response(200, json={"choices": []})
    await post(h, chat())
    (ev,) = h.events
    assert ev.input_tokens is None and ev.output_tokens is None


SSE = (
    b'data: {"choices":[{"delta":{"content":"he"}}]}\n\n'
    b'data: {"choices":[{"delta":{"content":"llo"}}]}\n\n'
    b'data: {"choices":[],"usage":{"prompt_tokens":21,"completion_tokens":9}}\n\n'
    b"data: [DONE]\n\n"
)


class Chunked(httpx.AsyncByteStream):
    def __init__(self, data: bytes, fail_after: bool = False) -> None:
        self._data = data
        self._fail = fail_after

    async def __aiter__(self):
        for i in range(0, len(self._data), 7):  # split mid-line on purpose
            yield self._data[i : i + 7]
        if self._fail:
            raise httpx.ReadError("boom-secret-detail")


async def test_stream_passthrough_byte_identical(make_harness):
    h = await make_harness()
    h.handler = lambda r: httpx.Response(
        200, headers={"content-type": "text/event-stream"}, stream=Chunked(SSE)
    )
    r = await post(h, chat(stream=True))
    assert r.status == 200
    assert r.headers["Content-Type"].startswith("text/event-stream")
    assert r.headers["Cache-Control"] == "no-cache"
    assert await r.read() == SSE
    (ev,) = h.events
    assert ev.status == UsageStatus.SUCCESS
    assert (ev.input_tokens, ev.output_tokens) == (21, 9)
    assert ev.task_id == "task-1"
    assert h.requests[0].headers["accept"] == "text/event-stream"


async def test_stream_upstream_breaks_midway_is_unknown_outcome(make_harness):
    h = await make_harness()
    h.handler = lambda r: httpx.Response(
        200, headers={"content-type": "text/event-stream"}, stream=Chunked(SSE[:40], True)
    )
    try:
        r = await post(h, chat(stream=True))
        await r.read()
    except Exception:  # connection dropped mid-body is the expected client-side symptom
        pass
    (ev,) = h.events
    assert ev.status == UsageStatus.UNKNOWN_OUTCOME


async def test_429_passthrough_sets_cooldown_and_error_row(make_harness):
    h = await make_harness()
    h.handler = lambda r: httpx.Response(
        429,
        json={"error": {"message": "slow down", "code": "rate_limit_exceeded"}},
        headers={"retry-after": "30"},
    )
    r = await post(h, chat())
    assert r.status == 429
    assert r.headers["Retry-After"] == "30"
    body = await r.json()
    assert body["error"]["type"] == "upstream_error"
    assert body["error"]["code"] == "rate_limit_exceeded"
    (ev,) = h.events
    assert ev.status == UsageStatus.ERROR
    assert h.cooldowns.is_cooling("groq", "q-x")
    assert not h.cooldowns.is_cooling("groq", "q-y")


async def test_cooling_model_rejected_without_upstream_call(make_harness):
    h = await make_harness()
    h.cooldowns.set_cooldown("groq", "q-x", NOW + timedelta(seconds=41.2))
    r = await post(h, chat())
    assert r.status == 429
    assert r.headers["Retry-After"] == "42"
    assert (await r.json())["error"]["code"] == "rate_limited"
    assert h.requests == [] and h.events == []
    # a provider-wide cooldown also blocks; the longest of the two wins
    h.cooldowns.set_cooldown("groq", None, NOW + timedelta(seconds=100))
    r = await post(h, chat("groq/q-y"))
    assert r.status == 429 and r.headers["Retry-After"] == "100"
    assert h.requests == []


async def test_503_upstream_sets_provider_cooldown(make_harness):
    h = await make_harness()
    h.handler = lambda r: httpx.Response(503, json={"error": {"message": "down"}})
    r = await post(h, chat())
    assert r.status == 503
    assert h.cooldowns.is_cooling("groq", "q-y")


async def test_redirect_not_followed(make_harness):
    h = await make_harness()
    h.handler = lambda r: httpx.Response(302, headers={"location": "https://evil.example/x"})
    r = await post(h, chat())
    assert r.status == 502
    assert len(h.requests) == 1
    assert (await r.json())["error"]["type"] == "upstream_error"
    assert h.events[0].status == UsageStatus.ERROR


async def test_connect_error_502_no_usage_row(make_harness):
    h = await make_harness()

    def boom(request):
        raise httpx.ConnectError("secret-detail-xyz")

    h.handler = boom
    r = await post(h, chat())
    assert r.status == 502
    text = json.dumps(await r.json())
    assert "secret-detail-xyz" not in text
    assert "ConnectError" in text
    assert h.events == []


async def test_read_timeout_504_unknown_outcome(make_harness):
    h = await make_harness()

    def boom(request):
        raise httpx.ReadTimeout("secret-detail-xyz")

    h.handler = boom
    r = await post(h, chat())
    assert r.status == 504
    body = await r.json()
    assert body["error"]["code"] == "upstream_timeout"
    assert "secret-detail-xyz" not in json.dumps(body)
    (ev,) = h.events
    assert ev.status == UsageStatus.UNKNOWN_OUTCOME


async def test_upstream_error_echoing_key_and_token_is_redacted(make_harness):
    h = await make_harness()
    h.handler = lambda r: httpx.Response(
        401,
        json={"error": {"message": f"bad key {GQ} and {TOKEN}", "code": f"c-{TOKEN}"}},
    )
    r = await post(h, chat())
    text = await r.text()
    assert GQ not in text and TOKEN not in text and SECRET not in text
    assert "[REDACTED]" in text


async def test_error_body_capped(make_harness):
    h = await make_harness()
    big = '{"error":{"message":"' + "A" * 5_000_000 + '"}}'
    h.handler = lambda r: httpx.Response(500, content=big.encode())
    r = await post(h, chat())
    assert r.status == 500
    assert len(await r.read()) < 2000  # truncated JSON falls back to "HTTP 500"


async def test_missing_key_env_503(make_harness):
    h = await make_harness(environ={"GROQ_API_KEY": ""})
    r = await post(h, chat())
    assert r.status == 503
    assert (await r.json())["error"]["code"] == "upstream_credential_missing"
    assert h.requests == []


async def test_oversized_request_413_and_bad_json_400(make_harness):
    h = await make_harness(gateway={"max_request_bytes": 65_536})
    r = await post(h, chat(user="x" * 70_000))
    assert r.status == 413
    assert h.requests == []
    for bad in (b"{nope", b"[1,2]", b'"str"'):
        r = await post(h, bad)
        assert r.status == 400
    assert h.requests == []


async def test_oversized_non_stream_response_502(make_harness):
    h = await make_harness(gateway={"max_response_bytes": 65_536})
    h.handler = lambda r: httpx.Response(200, content=b"x" * 100_000)
    r = await post(h, chat())
    assert r.status == 502
    assert (await r.json())["error"]["code"] == "upstream_response_too_large"
    (ev,) = h.events
    assert ev.status == UsageStatus.UNKNOWN_OUTCOME


async def test_models_endpoint_lists_only_allowlist(make_harness):
    h = await make_harness()
    r = await h.client.get("/v1/models", headers=AUTH)
    assert r.status == 200
    body = await r.json()
    assert body["object"] == "list"
    assert [m["id"] for m in body["data"]] == [
        "openrouter/openrouter/free",
        "gemini/g-3.8",
        "gemini/g-3.7",
        "groq/q-x",
        "groq/q-y",
    ]
    assert all(m["owned_by"] == "broke" and m["object"] == "model" for m in body["data"])


async def test_unknown_path_and_method_404(make_harness):
    h = await make_harness()
    for method, path in [
        ("GET", "/v1/chat/completions"),
        ("POST", "/v1/models"),
        ("GET", "/"),
        ("POST", "/v1/embeddings"),
        ("DELETE", "/v1/models"),
    ]:
        r = await h.client.request(method, path, headers=AUTH)
        assert r.status == 404, (method, path)
        assert "error" in await r.json()
    assert h.requests == []


async def test_usage_sink_failure_does_not_break_request(make_harness, caplog):
    h = await make_harness()

    def bad_sink(ev):
        raise RuntimeError("sink-secret-text")

    h.gw._usage_sink = bad_sink
    with caplog.at_level(logging.WARNING):
        r = await post(h, chat())
    assert r.status == 200
    assert "RuntimeError" in caplog.text and "sink-secret-text" not in caplog.text


async def test_logs_do_not_contain_secrets_or_bodies(make_harness, caplog):
    h = await make_harness()
    with caplog.at_level(logging.DEBUG):
        await post(h, chat(user="private-prompt-marker"))
    assert TOKEN not in caplog.text and GQ not in caplog.text
    assert "private-prompt-marker" not in caplog.text
    assert "groq/q-x" in caplog.text


async def test_real_start_binds_loopback_and_stop_idempotent(make_config):
    config = make_config()
    gw = LlmGateway(
        config,
        adapters={},
        cooldowns=CooldownManager(config.cooldown),
        usage_sink=lambda ev: None,
        current_task_id=lambda: None,
    )
    with pytest.raises(RuntimeError):
        _ = gw.base_url
    assert len(gw.token) >= 32
    await gw.start()
    try:
        assert gw._runner is not None
        host, port = gw._runner.addresses[0][:2]
        assert host == "127.0.0.1"
        assert gw.base_url == f"http://127.0.0.1:{port}/v1"
    finally:
        await gw.stop()
        await gw.stop()


async def test_concurrency_semaphore_configured(make_config):
    config = make_config(gateway={"max_concurrent_requests": 2})
    gw = LlmGateway(
        config,
        adapters={},
        cooldowns=CooldownManager(config.cooldown),
        usage_sink=lambda ev: None,
        current_task_id=lambda: None,
    )
    assert gw._sem._value == 2
    await gw.stop()
