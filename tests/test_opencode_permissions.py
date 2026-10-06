"""T060 minimal: OpenCode permission deny list, config validation and fail-closed preflight."""

from __future__ import annotations

import copy
import json

import httpx
import pytest
from oc_helpers import GATEWAY_TOKEN, GATEWAY_URL, good_config
from pydantic import ValidationError

from app.config.models import OpenCodeConfig, OpenCodePermissionConfig
from app.opencode.config_check import check_server_config, opencode_model_ids
from app.opencode.endpoint import ServerEndpoint
from app.opencode.executor import OpenCodeExecutor
from app.opencode.permissions import (
    DEFAULT_BASH_DENY,
    DENIED_TOOLS,
    agent_permission_problem,
    bash_deny_patterns,
    build_permission_block,
    default_permission_block,
    permission_problem,
)
from app.opencode.server import build_opencode_config
from app.runtime.interfaces import SmallModelCheck

ENDPOINT = ServerEndpoint("http://127.0.0.1:4096/")
EXTRA = "make deploy*"


def block(*extras: str) -> dict:
    return build_permission_block(OpenCodePermissionConfig(extra_bash_deny=list(extras)))


def body_with(perm) -> dict:
    return {"permission": perm}


# ---- block shape ------------------------------------------------------------------------


def test_block_shape_star_first_and_everything_denied():
    b = default_permission_block()
    bash = b["bash"]
    assert next(iter(bash)) == "*" and bash["*"] == "allow"
    for p in DEFAULT_BASH_DENY:
        assert bash[p] == "deny"
    for tool in DENIED_TOOLS:
        assert b[tool] == "deny"
    assert len(bash) == len(DEFAULT_BASH_DENY) + 1


def test_star_is_first_in_dumped_json():
    text = json.dumps(default_permission_block())
    assert text.startswith('{"bash": {"*": "allow", "*git push*": "deny"')


def test_defaults_contain_no_secret_like_text():
    for p in DEFAULT_BASH_DENY:
        assert "sk-" not in p and "API_KEY" not in p


def test_extras_appended_after_defaults_and_deduplicated():
    cfg = OpenCodePermissionConfig(extra_bash_deny=[EXTRA, "git clean*", EXTRA, "other*"])
    pats = bash_deny_patterns(cfg)
    assert pats[: len(DEFAULT_BASH_DENY)] == DEFAULT_BASH_DENY
    assert pats[len(DEFAULT_BASH_DENY) :] == (EXTRA, "other*")
    assert len(set(pats)) == len(pats)


def test_build_opencode_config_contains_block(make_config):
    cfg = make_config(opencode={"permission": {"extra_bash_deny": [EXTRA]}})
    out = build_opencode_config(cfg, gateway_base_url=GATEWAY_URL, gateway_token=GATEWAY_TOKEN)
    assert out["permission"] == build_permission_block(cfg.opencode.permission)
    assert out["permission"]["bash"][EXTRA] == "deny"
    assert list(out).index("permission") == list(out).index("small_model") + 1
    dumped = json.dumps(out["permission"])
    assert "sk-" not in dumped and "API_KEY" not in dumped


# ---- validator --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad",
    ["", "   ", "*", " * ", "a{env:X}", "a}", "a\nb", "a\x00b", "a\x7fb", "x" * 201],
)
def test_extra_bash_deny_rejections(bad):
    with pytest.raises(ValidationError):
        OpenCodePermissionConfig(extra_bash_deny=[bad])


def test_extra_bash_deny_ok_and_stripped():
    assert OpenCodePermissionConfig(extra_bash_deny=["  x*  ", "y" * 200]).extra_bash_deny == [
        "x*",
        "y" * 200,
    ]


def test_unknown_key_rejected():
    with pytest.raises(ValidationError):
        OpenCodePermissionConfig(bash_deny=["x"])
    with pytest.raises(ValidationError):
        OpenCodeConfig(permission={"bash_deny": ["x"]})


# ---- permission_problem -----------------------------------------------------------------


def good():
    return {"permission": default_permission_block()}


def test_good_body_ok():
    assert permission_problem(good(), default_permission_block()) is None


@pytest.mark.parametrize("perm", [None, "deny", [], 3])
def test_permission_missing_or_not_dict(perm):
    assert permission_problem({"permission": perm}, default_permission_block()) == "permission"
    assert permission_problem({}, default_permission_block()) == "permission"


@pytest.mark.parametrize("tool", DENIED_TOOLS)
@pytest.mark.parametrize("value", ["allow", "ask", None, True])
def test_denied_tools_must_be_deny(tool, value):
    b = good()
    b["permission"][tool] = value
    assert permission_problem(b, default_permission_block()) == f"permission.{tool}"
    del b["permission"][tool]
    assert permission_problem(b, default_permission_block()) == f"permission.{tool}"


def test_bash_not_dict():
    b = good()
    b["permission"]["bash"] = "deny"
    assert permission_problem(b, default_permission_block()) == "permission.bash"


def test_bash_star_not_first_is_unsafe():
    b = good()
    bash = b["permission"]["bash"]
    b["permission"]["bash"] = {**{k: v for k, v in bash.items() if k != "*"}, "*": "allow"}
    assert permission_problem(b, default_permission_block()) == "permission.bash"


@pytest.mark.parametrize("value", ["ask", "allow", None, 1])
def test_bash_non_deny_value_anywhere_is_unsafe(value):
    b = good()
    first_deny = next(k for k in b["permission"]["bash"] if k != "*")
    b["permission"]["bash"][first_deny] = value
    assert permission_problem(b, default_permission_block()) == "permission.bash"


def test_bash_leading_star_may_be_any_string():
    for v in ("allow", "ask", "deny"):
        b = good()
        b["permission"]["bash"]["*"] = v
        assert permission_problem(b, default_permission_block()) is None


def test_bash_missing_required_pattern():
    b = good()
    del b["permission"]["bash"][DEFAULT_BASH_DENY[5]]
    assert permission_problem(b, default_permission_block()) == "permission.bash"


def test_superset_ok_and_subset_unsafe():
    with_extra = {"permission": block(EXTRA)}
    assert permission_problem(with_extra, default_permission_block()) is None
    assert permission_problem(good(), block(EXTRA)) == "permission.bash"


def test_problem_detail_never_contains_pattern_text():
    out = permission_problem(good(), block(EXTRA))
    assert out == "permission.bash" and EXTRA not in out


@pytest.mark.parametrize("section", ["agent", "mode"])
def test_agent_permission_cases(section):
    ok = {"build": {"permission": {"edit": "deny"}}}
    assert agent_permission_problem(section, ok) is None
    assert agent_permission_problem(section, "nope") is None
    assert agent_permission_problem(section, {"x": "not a dict"}) is None
    assert agent_permission_problem(section, {"x": {"model": "m"}}) is None
    assert agent_permission_problem(section, {"x": {"permission": "allow"}}) == (
        f"{section}.x.permission"
    )
    assert agent_permission_problem(section, {"x": {"permission": {"websearch": "allow"}}}) == (
        f"{section}.x.permission.websearch"
    )
    assert agent_permission_problem(section, {"x": {"permission": {"bash": "allow"}}}) == (
        f"{section}.x.permission.bash"
    )
    assert agent_permission_problem(section, {"x": {"permission": {"bash": {"a*": "ask"}}}}) == (
        f"{section}.x.permission.bash"
    )
    assert agent_permission_problem(section, {"x": {"permission": {"bash": "deny"}}}) is None
    assert (
        agent_permission_problem(section, {"x": {"permission": {"bash": {"a*": "deny"}}}}) is None
    )


@pytest.mark.parametrize("section", ["agent", "mode"])
def test_permission_problem_checks_agent_sections(section):
    b = good()
    b[section] = {"build": {"permission": {"bash": {"git push*": "allow"}}}}
    assert permission_problem(b, default_permission_block()) == (f"{section}.build.permission.bash")
    b[section] = {"build": {"permission": {"edit": "deny"}}}
    assert permission_problem(b, default_permission_block()) is None


# ---- check_server_config / executor ------------------------------------------------------


async def probe(body, allowed, **kw):
    return await check_server_config(
        ENDPOINT,
        allowed,
        gateway_base_url=GATEWAY_URL,
        timeout_s=1,
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json=body)),
        environ={},
        **kw,
    )


async def test_check_server_config_unsafe_without_pattern_text(make_config):
    cfg = make_config()
    allowed = opencode_model_ids(cfg)
    body = copy.deepcopy(good_config(cfg))
    assert (await probe(body, allowed))[0] == SmallModelCheck.OK
    del body["permission"]
    out, detail = await probe(body, allowed)
    assert out == SmallModelCheck.UNSAFE and "permission" in detail
    body = copy.deepcopy(good_config(cfg))
    body["permission"]["bash"]["*git push*"] = "ask"
    out, detail = await probe(body, allowed)
    assert out == SmallModelCheck.UNSAFE and "permission.bash" in detail
    assert "git push" not in detail
    body = copy.deepcopy(good_config(cfg))
    body["permission"]["websearch"] = "allow"
    out, detail = await probe(body, allowed)
    assert (out, "permission.websearch" in detail) == (SmallModelCheck.UNSAFE, True)


async def test_check_server_config_required_permission_param(make_config):
    cfg = make_config()
    allowed = opencode_model_ids(cfg)
    body = good_config(cfg)
    assert (await probe(body, allowed, required_permission=block(EXTRA)))[0] == (
        SmallModelCheck.UNSAFE
    )
    cfg2 = make_config(opencode={"permission": {"extra_bash_deny": [EXTRA]}})
    body2 = good_config(cfg2)
    assert (await probe(body2, allowed))[0] == SmallModelCheck.OK  # superset of defaults


async def test_executor_requires_configured_extras(make_config):
    cfg = make_config(opencode={"permission": {"extra_bash_deny": [EXTRA]}})
    allowed = opencode_model_ids(cfg)
    defaults_only = good_config(make_config())
    ex = OpenCodeExecutor(
        cfg.opencode,
        endpoint=lambda: ENDPOINT,
        gateway_base_url=lambda: GATEWAY_URL,
        environ={},
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json=defaults_only)),
    )
    out, detail = await ex.check_server_config(allowed)
    assert out == SmallModelCheck.UNSAFE
    assert "does not deny the required operations" in detail and EXTRA not in detail
    ex2 = OpenCodeExecutor(
        cfg.opencode,
        endpoint=lambda: ENDPOINT,
        gateway_base_url=lambda: GATEWAY_URL,
        environ={},
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json=good_config(cfg))),
    )
    assert (await ex2.check_server_config(allowed))[0] == SmallModelCheck.OK


# ---- audit additions: defaults are immutable, preflight end to end ------------------------


REQUIRED_CATEGORY_SAMPLES = (
    "*git push*",
    "rm -r*",
    "rm * -r*",
    "git clean*",
    "kubectl *",
    "terraform apply*",
    "sudo*",
    "npm install -g*",
    "pip install --user*",
    "git config --global*",
    "ssh-keygen*",
    "npm publish*",
)


def test_default_list_exact_head_and_samples_and_no_duplicates():
    assert DEFAULT_BASH_DENY[:3] == ("*git push*", "gh auth*", "gh repo delete*")
    assert DEFAULT_BASH_DENY[-2:] == ("passwd*", "chpasswd*")
    assert len(DEFAULT_BASH_DENY) == len(set(DEFAULT_BASH_DENY))
    assert "*" not in DEFAULT_BASH_DENY
    assert isinstance(DEFAULT_BASH_DENY, tuple) and isinstance(DENIED_TOOLS, tuple)
    assert set(DENIED_TOOLS) == {"external_directory", "websearch", "doom_loop"}
    for p in REQUIRED_CATEGORY_SAMPLES:
        assert p in DEFAULT_BASH_DENY, p


def test_config_cannot_remove_or_override_defaults():
    # extras only add; an extra equal to a default stays a single deny entry in its place
    cfg = OpenCodePermissionConfig(extra_bash_deny=["*git push*", "sudo*"])
    pats = bash_deny_patterns(cfg)
    assert pats == DEFAULT_BASH_DENY
    b = build_permission_block(cfg)["bash"]
    assert b["*git push*"] == "deny" and b["sudo*"] == "deny" and next(iter(b)) == "*"
    # no config key exists that could carry an allow list or a removal
    for key in ("bash_allow", "bash_deny", "allow", "remove_defaults", "disable", "bash"):
        with pytest.raises(ValidationError):
            OpenCodePermissionConfig(**{key: ["x"]})
    # the shipped defaults object is unaffected by any config
    block("zzz*")
    assert "zzz*" not in DEFAULT_BASH_DENY


def test_generated_config_has_no_secret_like_text_for_whole_permission_block(make_config):
    cfg = make_config(opencode={"permission": {"extra_bash_deny": [EXTRA]}})
    dumped = json.dumps(build_permission_block(cfg.opencode.permission))
    for needle in ("sk-", "API_KEY", "TOKEN", "{env:", "{file:"):
        assert needle not in dumped


def test_extra_cannot_be_allow_value_even_if_crafted():
    # extras are patterns only; every extra gets the value "deny"
    b = block("foo*", "bar*")["bash"]
    assert b["foo*"] == "deny" and b["bar*"] == "deny"
    assert list(b)[-2:] == ["foo*", "bar*"]


async def test_preflight_end_to_end_mutations_are_unsafe(make_config):
    cfg = make_config()
    allowed = opencode_model_ids(cfg)

    def fresh():
        return copy.deepcopy(good_config(cfg))

    assert (await probe(fresh(), allowed))[0] == SmallModelCheck.OK
    cases = {}

    b = fresh()
    del b["permission"]
    cases["missing permission"] = (b, "permission")

    b = fresh()  # reordered: the allow-all moved last, so it would override every deny
    bash = b["permission"]["bash"]
    b["permission"]["bash"] = {**{k: v for k, v in bash.items() if k != "*"}, "*": "allow"}
    cases["star last"] = (b, "permission.bash")

    b = fresh()  # a later allow overriding one deny
    b["permission"]["bash"]["*git push*"] = "allow"
    cases["later allow"] = (b, "permission.bash")

    b = fresh()  # an extra allow rule appended after the denies
    b["permission"]["bash"]["git push origin*"] = "allow"
    cases["appended allow"] = (b, "permission.bash")

    b = fresh()
    del b["permission"]["bash"]["sudo*"]
    cases["missing pattern"] = (b, "permission.bash")

    for tool in DENIED_TOOLS:
        for value in ("allow", "ask"):
            b = fresh()
            b["permission"][tool] = value
            cases[f"{tool}={value}"] = (b, f"permission.{tool}")

    b = fresh()
    b["agent"] = {"build": {"permission": {"bash": "allow"}}}
    cases["agent bash allow"] = (b, "agent.build.permission.bash")

    b = fresh()
    b["mode"] = {"plan": {"permission": {"websearch": "allow"}}}
    cases["mode websearch allow"] = (b, "mode.plan.permission.websearch")

    for label, (body, path) in cases.items():
        out, detail = await probe(body, allowed)
        assert out == SmallModelCheck.UNSAFE, label
        assert f"'{path}'" in detail, (label, detail)
        assert "does not deny the required operations" in detail
        for pattern in DEFAULT_BASH_DENY:
            assert pattern not in detail, (label, pattern)
        assert "allow" not in detail and "ask" not in detail


async def test_preflight_fail_closed_when_permission_missing_even_with_empty_required(make_config):
    cfg = make_config()
    body = copy.deepcopy(good_config(cfg))
    del body["permission"]
    out, _ = await probe(body, opencode_model_ids(cfg), required_permission=None)
    assert out == SmallModelCheck.UNSAFE


@pytest.mark.parametrize("value", ["ask", "allow", None, True, 0])
def test_non_required_appended_rule_must_also_be_deny(value):
    # last match wins: any rule after the leading "*" that is not a deny could re-allow a command
    b = good()
    b["permission"]["bash"]["some unknown command*"] = value
    assert permission_problem(b, default_permission_block()) == "permission.bash"
    b["permission"]["bash"]["some unknown command*"] = "deny"
    assert permission_problem(b, default_permission_block()) is None
