# ruff: noqa: F811
"""Tester additions for the gateway: free-only, auth, secrets and SSE edge cases."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

import aiohttp
import httpx
import pytest
from conftest import secret_env
from test_gateway import (  # noqa: F401  (make_harness is a fixture)
    AUTH,
    GQ,
    OK_BODY,
    TOKEN,
    Chunked,
    chat,
    make_harness,
    post,
)

import app.gateway.server as gw_mod
from app.db.usage import UsageStatus
from app.gateway.server import ALLOWED_REQUEST_KEYS, LlmGateway, _SseUsage
from app.providers.base import CostClass
from app.quota.cooldown import CooldownManager

ALL_SECRETS = [v for v in secret_env().values() if v]


# ---- auth ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "header",
    [
        f"bearer {TOKEN}",
        f"BEARER {TOKEN}",
        f"Bearer {TOKEN.upper()}",
        f"Bearer {TOKEN.lower()}",
        f"Bearer  {TOKEN}",
        f"Bearer {TOKEN[:-1]}",
        f"Bearer {TOKEN}x",
        f"Basic {TOKEN}",
        "Bearer ",
        "Bearer",
        "",
    ],
)
async def test_token_must_match_exactly(make_harness, header):
    h = await make_harness()
    r = await post(h, chat(), headers={"Authorization": header})
    assert r.status == 401
    r = await h.client.get("/v1/models", headers={"Authorization": header})
    assert r.status == 401
    assert h.requests == [] and h.events == []


async def test_oversized_body_with_bad_token_is_401_not_413(make_harness):
    h = await make_harness(gateway={"max_request_bytes": 65_536})
    r = await post(h, chat(user="x" * 200_000), headers={"Authorization": "Bearer nope"})
    assert r.status == 401
    assert h.requests == []


async def test_token_compare_is_constant_time_primitive(make_harness, monkeypatch):
    h = await make_harness()
    calls: list[tuple[Any, Any]] = []
    real = gw_mod.hmac.compare_digest

    def spy(a, b):
        calls.append((a, b))
        return real(a, b)

    monkeypatch.setattr(gw_mod.hmac, "compare_digest", spy)
    await post(h, chat(), headers={"Authorization": "Bearer nope"})
    assert calls and all(isinstance(a, bytes) and isinstance(b, bytes) for a, b in calls)


async def test_non_ascii_authorization_header_is_401_not_500(make_harness):
    h = await make_harness()
    r = await h.client.post(
        "/v1/chat/completions",
        json=chat(),
        headers={"Authorization": "Bearer éé".encode().decode("latin-1")},
    )
    assert r.status == 401


# ---- allowlist -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "model",
    [
        "GROQ/q-x",
        "groq/Q-X",
        "groq/q-x ",
        " groq/q-x",
        "groq/q-x\n",
        "groq/q-x/",
        "groq//q-x",
        "groq/q-x\x00",
        "groq/q-x?x=1",
        "groq/q-",
        "groq/",
        "groq",
        "/groq/q-x",
        "groq∕q-x",  # unicode division slash
        "groq/q‑x",  # non-breaking hyphen
        "openrouter/free",  # upstream id without gateway prefix duplication
        "openrouter/openrouter/free:paid",
        "openrouter/openrouter/free ",
        "",
        "*",
        True,
        {"id": "groq/q-x"},
    ],
)
async def test_lookalike_model_ids_rejected_without_upstream_call(make_harness, model):
    h = await make_harness()
    r = await post(h, {"model": model, "messages": []})
    assert r.status == 403
    assert (await r.json())["error"]["code"] == "model_not_allowlisted"
    assert h.requests == [] and h.events == []


async def test_missing_model_key_rejected(make_harness):
    h = await make_harness()
    r = await post(h, {"messages": []})
    assert r.status == 403 and h.requests == []


async def test_model_is_never_substituted_when_cooling_or_missing_key(make_harness):
    h = await make_harness(environ={"GROQ_API_KEY": ""})
    r = await post(h, chat("groq/q-x"))
    assert r.status == 503
    assert h.requests == []  # no fallback to another provider/model


# ---- body whitelist ------------------------------------------------------------------------


async def test_every_allowed_key_is_forwarded_and_nothing_else(make_harness):
    h = await make_harness()
    body = {k: [] if k in {"messages", "tools", "stop"} else 1 for k in ALLOWED_REQUEST_KEYS}
    body["stream"] = False
    body["model"] = "groq/q-x"
    extras = {
        "models": ["paid/x"],
        "provider": {"order": ["x"], "allow_fallbacks": True},
        "plugins": [],
        "route": "fallback",
        "transforms": ["middle-out"],
        "extra_body": {"model": "paid/x"},
        "base_url": "https://evil.example",
        "url": "https://evil.example",
        "api_key": "client-key",
        "headers": {"Authorization": "Bearer x"},
        "Model": "paid/x",
        "MODELS": ["paid/y"],
        "": 1,
    }
    r = await post(h, {**body, **extras})
    assert r.status == 200
    sent = json.loads(h.requests[0].content)
    assert set(sent) == set(ALLOWED_REQUEST_KEYS) | {"model"}
    assert sent["model"] == "q-x"
    assert len(h.requests) == 1


async def test_dropped_key_values_never_logged(make_harness, caplog):
    h = await make_harness()
    with caplog.at_level(logging.DEBUG):
        await post(h, chat(plugins=[{"id": "super-secret-plugin-value"}], route="r-secret-9"))
    assert "super-secret-plugin-value" not in caplog.text
    assert "r-secret-9" not in caplog.text
    assert "plugins" in caplog.text


# ---- redirects, upstream URL ---------------------------------------------------------------


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
async def test_all_redirects_become_502_with_one_upstream_call(make_harness, status):
    h = await make_harness()
    h.handler = lambda r: httpx.Response(
        status, headers={"location": "https://api.groq.com/other", "set-cookie": "a=b"}
    )
    r = await post(h, chat())
    assert r.status == 502
    assert len(h.requests) == 1
    assert "location" not in {k.lower() for k in r.headers}
    assert "set-cookie" not in {k.lower() for k in r.headers}
    (ev,) = h.events
    assert ev.status == UsageStatus.ERROR and ev.cost_class == CostClass.FREE


async def test_upstream_response_headers_not_relayed(make_harness):
    h = await make_harness()
    h.handler = lambda r: httpx.Response(
        200,
        json=OK_BODY,
        headers={"set-cookie": "s=1", "x-secret-hdr": "v", "content-encoding": "gzip"},
    )
    r = await post(h, chat())
    lower = {k.lower() for k in r.headers}
    assert "set-cookie" not in lower and "x-secret-hdr" not in lower
    assert "content-encoding" not in lower
    assert h.requests[0].headers["accept-encoding"] == "identity"


# ---- usage rows ----------------------------------------------------------------------------


async def test_all_usage_rows_are_free_across_outcomes(make_harness):
    h = await make_harness()
    outcomes = [
        lambda r: httpx.Response(200, json=OK_BODY),
        lambda r: httpx.Response(429, json={"error": {"message": "x"}}),
        lambda r: httpx.Response(500, json={"error": {"message": "x"}}),
        lambda r: httpx.Response(302, headers={"location": "https://x/"}),
        lambda r: httpx.Response(200, content=b"not-json"),
    ]
    for i, handler in enumerate(outcomes):
        h.handler = handler
        h.cooldowns = h.gw._cooldowns = CooldownManager(h.gw._config.cooldown)  # reset cooldowns
        await post(h, chat("groq/q-y" if i % 2 else "groq/q-x"))
    assert len(h.events) == len(outcomes)
    assert all(ev.cost_class == CostClass.FREE for ev in h.events)
    assert all(ev.task_id == "task-1" for ev in h.events)
    assert {ev.provider for ev in h.events} == {"groq"}


async def test_rejected_requests_write_no_usage_rows(make_harness):
    h = await make_harness()
    await post(h, chat("groq/nope"))
    await post(h, b"{bad")
    await post(h, chat(), headers={})
    h.cooldowns.set_cooldown("groq", None, h.gw._clock().replace(year=2100))
    await post(h, chat())
    assert h.events == [] and h.requests == []


async def test_usage_task_id_is_read_per_request(make_harness):
    h = await make_harness()
    await post(h, chat())
    h.task_id = None
    await post(h, chat())
    h.task_id = "task-2"
    await post(h, chat())
    assert [e.task_id for e in h.events] == ["task-1", None, "task-2"]


async def test_non_stream_invalid_usage_values_ignored(make_harness):
    h = await make_harness()
    h.handler = lambda r: httpx.Response(
        200, json={"usage": {"prompt_tokens": -1, "completion_tokens": "x"}}
    )
    await post(h, chat())
    h.handler = lambda r: httpx.Response(
        200, json={"usage": {"prompt_tokens": True, "completion_tokens": 3}}
    )
    await post(h, chat())
    assert [(e.input_tokens, e.output_tokens) for e in h.events] == [(None, None)] * 2


# ---- secrets -------------------------------------------------------------------------------


async def test_provider_keys_never_in_any_response_log_or_usage_row(make_harness, caplog):
    h = await make_harness()
    scenarios = [
        lambda r: httpx.Response(200, json=OK_BODY, headers={"x-request-id": "rq1"}),
        lambda r: httpx.Response(401, text=f"bad credentials {GQ}"),
        lambda r: httpx.Response(429, json={"error": {"message": f"key {GQ} limited"}}),
        lambda r: httpx.Response(500, content=f"<html>{GQ}</html>".encode()),
        lambda r: httpx.Response(
            400, json={"error": {"message": f"{TOKEN}", "code": GQ, "type": GQ}}
        ),
    ]
    texts: list[str] = []
    with caplog.at_level(logging.DEBUG):
        for handler in scenarios:
            h.handler = handler
            h.cooldowns.clear() if hasattr(h.cooldowns, "clear") else None
            h.gw._cooldowns = h.cooldowns = CooldownManager(h.gw._config.cooldown)
            r = await post(h, chat())
            texts.append(await r.text() + json.dumps(dict(r.headers)))
    blob = "\n".join(texts) + caplog.text + repr(h.events)
    for secret in ALL_SECRETS + [TOKEN]:
        assert secret not in blob, secret[:6]
    # the real key is used exactly where it must be: the upstream Authorization header
    assert all(req.headers["authorization"] == f"Bearer {GQ}" for req in h.requests)


async def test_exception_text_from_transport_never_leaks(make_harness, caplog):
    h = await make_harness()

    def boom(request):
        raise httpx.ConnectError(f"cannot reach with {GQ} {TOKEN}")

    h.handler = boom
    with caplog.at_level(logging.DEBUG):
        r = await post(h, chat())
    assert GQ not in await r.text() and GQ not in caplog.text and TOKEN not in caplog.text


# ---- SSE parsing ---------------------------------------------------------------------------


def feed_all(data: bytes, step: int) -> _SseUsage:
    p = _SseUsage()
    for i in range(0, len(data), step):
        p.feed(data[i : i + step])
    return p


USAGE_LINE = b'data: {"usage":{"prompt_tokens":5,"completion_tokens":6}}\n\n'


@pytest.mark.parametrize("step", [1, 2, 3, 7, 1000])
def test_sse_usage_found_for_any_chunk_split(step):
    data = b'data: {"choices":[]}\n\n' + USAGE_LINE + b"data: [DONE]\n\n"
    assert feed_all(data, step).tokens == (5, 6)


def test_sse_crlf_and_last_usage_wins_and_bad_lines_ignored():
    data = (
        b'data: {"usage":{"prompt_tokens":1,"completion_tokens":2}}\r\n\r\n'
        b"data: not-json\r\n"
        b": comment\r\n"
        b'data: {"usage":null}\n'
        b'data: {"usage":{"prompt_tokens":-3,"completion_tokens":4}}\n'
        b"data: [1,2]\n"
        b'data: {"usage":{"prompt_tokens":8,"completion_tokens":9}}\n'
    )
    assert feed_all(data, 5).tokens == (8, 9)


def test_sse_line_buffer_is_bounded(monkeypatch):
    monkeypatch.setattr(gw_mod, "_LINE_BUFFER_CAP", 1000)
    p = _SseUsage()
    p.feed(b"data: " + b"A" * 5000)  # one endless line, no newline
    assert len(p._buf) <= 1000
    p.feed(USAGE_LINE)  # parsing is disabled after overflow; must not raise or grow
    assert len(p._buf) <= 1000
    assert p.tokens == (None, None)


async def test_stream_requested_but_upstream_returns_json(make_harness):
    h = await make_harness()
    r = await post(h, chat(stream=True))
    assert r.status == 200
    assert await r.json() == OK_BODY
    assert h.requests[0].headers["accept"] == "text/event-stream"
    assert h.events[0].status == UsageStatus.SUCCESS
    assert (h.events[0].input_tokens, h.events[0].output_tokens) == (11, 7)


async def test_not_stream_flag_with_sse_upstream_is_not_passed_as_stream(make_harness):
    h = await make_harness()
    h.handler = lambda r: httpx.Response(
        200, headers={"content-type": "text/event-stream"}, content=b"data: x\n\n"
    )
    r = await post(h, chat())  # stream not requested
    assert r.status == 200
    assert r.headers["Content-Type"].startswith("application/json")


async def test_stream_with_multibyte_split_is_byte_identical(make_harness):
    h = await make_harness()
    payload = ('data: {"choices":[{"delta":{"content":"héllo 世界"}}]}\n\n').encode()
    payload += USAGE_LINE + b"data: [DONE]\n\n"
    h.handler = lambda r: httpx.Response(
        200, headers={"content-type": "text/event-stream; charset=utf-8"}, stream=Chunked(payload)
    )
    r = await post(h, chat(stream=True))
    assert await r.read() == payload
    assert (h.events[0].input_tokens, h.events[0].output_tokens) == (5, 6)


# ---- real socket: binding, auth before body, size limit, disconnect -------------------------


async def _started_gateway(make_config, handler, **cfg):
    config = make_config(**cfg)
    from app.providers.registry import build_adapters

    transport = httpx.MockTransport(handler)
    events: list[Any] = []
    adapters = build_adapters(config, transport=transport, environ=secret_env())
    gw = LlmGateway(
        config,
        adapters=adapters,
        cooldowns=CooldownManager(config.cooldown),
        usage_sink=events.append,
        current_task_id=lambda: "t",
        transport=transport,
        token=TOKEN,
    )
    await gw.start()
    return gw, events


async def test_real_socket_only_loopback_auth_and_limits(make_config):
    seen: list[httpx.Request] = []

    def handler(req):
        seen.append(req)
        return httpx.Response(200, json=OK_BODY)

    gw, events = await _started_gateway(make_config, handler, gateway={"max_request_bytes": 65_536})
    try:
        host, port = gw._runner.addresses[0][:2]
        assert host == "127.0.0.1"
        assert len(gw._runner.addresses) == 1
        base = gw.base_url
        async with aiohttp.ClientSession() as s:
            big = json.dumps(chat(user="x" * 100_000))
            async with s.post(f"{base}/chat/completions", data=big) as r:
                assert r.status == 401  # no token: rejected before body size is evaluated
            async with s.post(f"{base}/chat/completions", data=big, headers=AUTH) as r:
                assert r.status == 413
            async with s.post(
                f"{base}/chat/completions", data=json.dumps(chat()), headers=AUTH
            ) as r:
                assert r.status == 200
            async with s.get(f"http://127.0.0.1:{port}/nope", headers=AUTH) as r:
                assert r.status == 404
        assert len(seen) == 1
    finally:
        await gw.stop()


async def test_client_disconnect_mid_stream_records_unknown_outcome(make_config):
    closed = asyncio.Event()

    class Endless(httpx.AsyncByteStream):
        async def __aiter__(self):
            try:
                while True:
                    yield b'data: {"choices":[]}\n\n' * 50
                    await asyncio.sleep(0.01)
            finally:
                closed.set()

    def handler(req):
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=Endless())

    gw, events = await _started_gateway(make_config, handler)
    try:
        async with aiohttp.ClientSession() as s:
            r = await s.post(
                f"{gw.base_url}/chat/completions", data=json.dumps(chat(stream=True)), headers=AUTH
            )
            await r.content.read(100)
            r.close()
        await asyncio.wait_for(closed.wait(), 10)  # upstream stream was closed
        for _ in range(100):
            if events:
                break
            await asyncio.sleep(0.05)
        assert len(events) == 1
        assert events[0].status == UsageStatus.UNKNOWN_OUTCOME
        assert events[0].cost_class == CostClass.FREE
    finally:
        await gw.stop()


async def test_gateway_token_scrubbed_even_without_a_redactor(make_config):
    def handler(req):
        return httpx.Response(
            400, json={"error": {"message": f"echo {TOKEN}", "code": f"c-{TOKEN}"}}
        )

    gw, _events = await _started_gateway(make_config, handler)  # no redactor passed
    try:
        async with aiohttp.ClientSession() as s:
            async with s.post(
                f"{gw.base_url}/chat/completions", data=json.dumps(chat()), headers=AUTH
            ) as r:
                text = await r.text()
        assert r.status == 400
        assert TOKEN not in text and "[REDACTED]" in text
    finally:
        await gw.stop()
