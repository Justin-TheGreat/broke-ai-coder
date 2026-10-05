from __future__ import annotations

import base64
import copy
import json

import httpx
import pytest
from oc_helpers import GATEWAY_URL, good_config

from app.opencode.config_check import check_server_config, opencode_model_ids
from app.opencode.endpoint import ServerEndpoint
from app.runtime.interfaces import SmallModelCheck

ENDPOINT = ServerEndpoint("http://127.0.0.1:4096/")


@pytest.fixture
def cfg(make_config):
    return make_config()


@pytest.fixture
def allowed(cfg):
    return opencode_model_ids(cfg)


async def probe(handler, allowed, *, endpoint=ENDPOINT, base=GATEWAY_URL, environ=None):
    return await check_server_config(
        endpoint,
        allowed,
        gateway_base_url=base,
        timeout_s=1,
        transport=httpx.MockTransport(handler),
        environ=environ,
    )


def respond(body):
    return lambda req: httpx.Response(200, json=body)


async def test_ok_url_and_good_config(cfg, allowed):
    seen = []

    def h(req):
        seen.append(str(req.url))
        return httpx.Response(200, json=good_config(cfg))

    assert await probe(h, allowed) == (SmallModelCheck.OK, "")
    assert seen == ["http://127.0.0.1:4096/config"]


async def test_basic_auth_is_sent(cfg, allowed):
    seen = []

    def h(req):
        seen.append(req.headers.get("authorization"))
        return httpx.Response(200, json=good_config(cfg))

    ep = ServerEndpoint("http://127.0.0.1:4096", "pw-secret-1")
    out, _ = await probe(h, allowed, endpoint=ep)
    assert out == SmallModelCheck.OK
    expected = "Basic " + base64.b64encode(b"opencode:pw-secret-1").decode()
    assert seen == [expected]


async def test_baseurl_check_skipped_when_unknown(cfg, allowed):
    body = good_config(cfg)
    body["provider"]["broke"]["options"]["baseURL"] = "http://elsewhere/v1"
    out, _ = await probe(respond(body), allowed, base=None)
    assert out == SmallModelCheck.OK


def mutated(cfg, fn):
    body = copy.deepcopy(good_config(cfg))
    fn(body)
    return body


UNSAFE_MUTATIONS = {
    # check 1: enabled_providers exactly ["broke"]
    "enabled_missing": (lambda b: b.pop("enabled_providers"), "enabled_providers"),
    "enabled_extra": (
        lambda b: b.__setitem__("enabled_providers", ["broke", "groq"]),
        "enabled_providers",
    ),
    "enabled_string": (lambda b: b.__setitem__("enabled_providers", "broke"), "enabled_providers"),
    "enabled_empty": (lambda b: b.__setitem__("enabled_providers", []), "enabled_providers"),
    # check 2: provider keys exactly {"broke"}
    "provider_missing": (lambda b: b.pop("provider"), "provider"),
    "provider_extra": (
        lambda b: b["provider"].__setitem__("openai", {"options": {"apiKey": "k"}}),
        "provider",
    ),
    "provider_not_dict": (lambda b: b.__setitem__("provider", ["broke"]), "provider"),
    # check 3
    "npm": (lambda b: b["provider"]["broke"].__setitem__("npm", "@ai-sdk/openai"), "npm"),
    "baseurl": (
        lambda b: b["provider"]["broke"]["options"].__setitem__("baseURL", "https://api.x/v1"),
        "baseURL",
    ),
    "baseurl_missing": (lambda b: b["provider"]["broke"].pop("options"), "baseURL"),
    "model_key_not_allowlisted": (
        lambda b: b["provider"]["broke"]["models"].__setitem__("openai/gpt-4o", {}),
        "models",
    ),
    "models_not_dict": (lambda b: b["provider"]["broke"].__setitem__("models", ["x"]), "models"),
    # check 4
    "small_missing": (lambda b: b.pop("small_model"), "small_model"),
    "small_paid": (lambda b: b.__setitem__("small_model", "openai/gpt-4o"), "small_model"),
    "small_not_prefixed": (lambda b: b.__setitem__("small_model", "groq/q-x"), "small_model"),
    "small_not_str": (lambda b: b.__setitem__("small_model", 5), "small_model"),
    # check 5
    "model_paid": (lambda b: b.__setitem__("model", "openai/gpt-4o"), "model"),
    "agent_paid": (lambda b: b.__setitem__("agent", {"x": {"model": "openai/gpt-4o"}}), "agent.x"),
    "mode_paid": (lambda b: b.__setitem__("mode", {"x": {"model": "openai/gpt-4o"}}), "mode.x"),
    "command_paid": (
        lambda b: b.__setitem__("command", {"x": {"model": "anthropic/claude"}}),
        "command.x.model",
    ),
    "command_not_dict": (lambda b: b.__setitem__("command", ["x"]), "command"),
}


@pytest.mark.parametrize("name", sorted(UNSAFE_MUTATIONS))
async def test_gateway_shape_violations_are_unsafe(cfg, allowed, name):
    mutate, key = UNSAFE_MUTATIONS[name]
    out, detail = await probe(respond(mutated(cfg, mutate)), allowed)
    assert out == SmallModelCheck.UNSAFE
    assert key in detail
    assert "openai/gpt-4o" not in detail and "api.x" not in detail


async def test_good_config_variants_with_allowlisted_agent_models(cfg, allowed):
    first = sorted(allowed)[0]
    body = mutated(
        cfg,
        lambda b: b.update(agent={"a": {"model": first}}, command={"c": {"model": first}}, mode={}),
    )
    out, _ = await probe(respond(body), allowed)
    assert out == SmallModelCheck.OK


async def test_empty_allowlist_is_unsafe(cfg):
    out, _ = await probe(respond(good_config(cfg)), frozenset())
    assert out == SmallModelCheck.UNSAFE


@pytest.mark.parametrize("status", [201, 204, 301, 401, 403, 404, 500, 503])
async def test_non_200_is_unavailable(cfg, allowed, status):
    out, detail = await probe(lambda r: httpx.Response(status, json=good_config(cfg)), allowed)
    assert (out, detail) == (SmallModelCheck.UNAVAILABLE, "GET /config failed")


@pytest.mark.parametrize("body", [None, "x", 5, [], [{"small_model": "x"}]])
async def test_non_dict_is_unavailable(allowed, body):
    out, detail = await probe(respond(body), allowed)
    assert (out, detail) == (SmallModelCheck.UNAVAILABLE, "GET /config failed")


async def test_unparseable_and_network_errors_are_unavailable(allowed):
    out, _ = await probe(lambda r: httpx.Response(200, content=b"nope"), allowed)
    assert out == SmallModelCheck.UNAVAILABLE

    def boom(r):
        raise httpx.ConnectError("x")

    assert (await probe(boom, allowed))[0] == SmallModelCheck.UNAVAILABLE


# ---- check 0: stored credentials ---------------------------------------------------------


async def test_stored_credentials_are_unsafe_without_contacting_server(cfg, allowed, tmp_path):
    store = tmp_path / "opencode"
    store.mkdir()
    (store / "auth.json").write_text(json.dumps({"groq": {"type": "api", "key": "sk-leak-123"}}))
    calls = []

    def h(req):
        calls.append(req)
        return httpx.Response(200, json=good_config(cfg))

    out, detail = await probe(h, allowed, environ={"XDG_DATA_HOME": str(tmp_path)})
    assert out == SmallModelCheck.UNSAFE
    assert "groq" in detail and "rm -f" in detail
    assert "sk-leak-123" not in detail
    assert calls == []


# ---- allowlist ---------------------------------------------------------------------------


def test_model_ids_use_gateway_provider_prefix(cfg):
    ids = opencode_model_ids(cfg)
    assert ids == {f"broke/{gid}" for gid in cfg.gateway_models()}
    assert "broke/openrouter/openrouter/free" in ids


def test_model_ids_skip_disabled(make_config):
    base = make_config()
    disabled = make_config(
        routing={
            "policies": {
                pid: {**p.model_dump(), "enabled": False}
                for pid, p in base.routing.policies.items()
            }
        }
    )
    assert opencode_model_ids(disabled) == frozenset()


async def test_auth_problem_callable_is_used_when_given(cfg, allowed, tmp_path):
    called = []

    async def problem():
        called.append(1)
        return "OpenCode has stored provider credentials for: openai. callable"

    def h(req):
        raise AssertionError("GET /config must not run after an auth problem")

    out, detail = await check_server_config(
        ENDPOINT,
        allowed,
        gateway_base_url=GATEWAY_URL,
        timeout_s=1,
        transport=httpx.MockTransport(h),
        auth_problem=problem,
    )
    assert out == SmallModelCheck.UNSAFE and "callable" in detail and called == [1]


async def test_auth_problem_callable_none_continues_to_config(cfg, allowed):
    async def problem():
        return None

    out, _ = await check_server_config(
        ENDPOINT,
        allowed,
        gateway_base_url=GATEWAY_URL,
        timeout_s=1,
        transport=httpx.MockTransport(respond(good_config(cfg))),
        auth_problem=problem,
    )
    assert out == SmallModelCheck.OK
