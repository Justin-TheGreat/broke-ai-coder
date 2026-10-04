from __future__ import annotations

import logging
from pathlib import Path

import pytest

from app.config.loader import ConfigError, load_config, parse_config
from app.config.models import is_free_openrouter_model
from app.config.secrets import credentials_present, resolve_secret

ROOT = Path(__file__).resolve().parent.parent


def test_example_config_loads():
    cfg = load_config(ROOT / "config.example.yaml")
    assert cfg.routing.provider_order


def test_empty_file_gives_defaults(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text("")
    cfg = load_config(p)
    assert cfg.routing.max_fallback_attempts == 3
    assert cfg.database.retention_days == 60


@pytest.mark.parametrize(
    "routing",
    [
        {"mode": "free-only"},
        {"mode": "free-first-paid-after-approval"},
        {"paid_requires_approval": True},
        {"daily_paid_budget_usd": 0},
        {"monthly_paid_budget_usd": None},
    ],
    ids=["mode-free-only", "mode-paid", "approval", "daily-budget", "monthly-budget"],
)
def test_paid_settings_rejected(routing):
    # Even "harmless" legacy values are refused so nobody believes paid is configurable.
    with pytest.raises(ConfigError, match="free-only"):
        parse_config({"routing": routing})


@pytest.mark.parametrize("cost_class", ["FREE", "PAID"])
def test_policy_cost_class_rejected(cost_class):
    data = {
        "providers": {"a": {"api_key_env": "A_KEY"}},
        "routing": {
            "policies": {"x": {"provider": "a", "cost_class": cost_class, "model_order": ["m"]}}
        },
    }
    with pytest.raises(ConfigError, match="free-only"):
        parse_config(data)


def test_yaml_paid_policy_rejected(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text(
        "providers:\n"
        "  openrouter: {api_key_env: OPENROUTER_API_KEY}\n"
        "routing:\n"
        "  provider_order: [openrouter-paid]\n"
        "  policies:\n"
        "    openrouter-paid: {provider: openrouter, cost_class: PAID,"
        " model_order: [anthropic/claude-opus]}\n"
    )
    with pytest.raises(ConfigError, match="free-only"):
        load_config(p)


@pytest.mark.parametrize("model", ["anthropic/claude-opus", "openai/gpt-5", "x/free", "m:freebie"])
def test_openrouter_non_free_model_rejected(model):
    data = {
        "providers": {"openrouter": {"api_key_env": "OPENROUTER_API_KEY"}},
        "routing": {"policies": {"or": {"provider": "openrouter", "model_order": [model]}}},
    }
    with pytest.raises(ConfigError, match="not a free variant"):
        parse_config(data)


def test_openrouter_free_variants_accepted():
    data = {
        "providers": {"openrouter": {"api_key_env": "OPENROUTER_API_KEY"}},
        "routing": {
            "provider_order": ["or"],
            "policies": {
                "or": {
                    "provider": "openrouter",
                    "model_order": ["openrouter/free", "qwen/qwen3-coder:free"],
                }
            },
        },
    }
    parse_config(data)
    assert is_free_openrouter_model("openrouter/free")
    assert not is_free_openrouter_model("openrouter/auto")


def test_free_variant_rule_only_applies_to_openrouter():
    # Other providers' model ids carry no price signal; their free-ness is an account setting.
    data = {
        "providers": {"groq": {"api_key_env": "GROQ_API_KEY"}},
        "routing": {
            "policies": {"g": {"provider": "groq", "model_order": ["openai/gpt-oss-120b"]}}
        },
    }
    parse_config(data)


def _pol(provider="groq", models=("m",), **kw):
    return {"provider": provider, "model_order": list(models), **kw}


def _data(routing, providers=("groq", "gemini")):
    return {
        "providers": {p: {"api_key_env": f"{p.upper()}_KEY"} for p in providers},
        "routing": routing,
    }


def test_valid_minimal():
    parse_config(_data({"provider_order": ["x"], "policies": {"x": _pol()}}))


@pytest.mark.parametrize(
    ("data", "match"),
    [
        (_data({"provider_order": ["nope"], "policies": {"x": _pol()}}), "unknown policy id"),
        (_data({"provider_order": ["x", "x"], "policies": {"x": _pol()}}), "duplicate entries"),
        (_data({"policies": {"x": _pol(models=("m", "m"))}}), "duplicate models"),
        (_data({"policies": {"x": _pol(), "y": _pol()}}), "appears in policies"),
        (_data({"policies": {"x": _pol(provider="undefined")}}), "undefined provider"),
        (_data({"policies": {"x": _pol(models=())}}), "model_order"),
        ({"providers": {"a": {"api_key_env": "sk-or-abc"}}}, "api_key_env"),
        ({"bogus": 1}, "bogus"),
        ({"routing": {"max_fallback_attempts": 0}}, "max_fallback_attempts"),
    ],
    ids=[
        "unknown-order-id",
        "dup-order",
        "dup-model",
        "same-pair-two-policies",
        "undefined-provider",
        "empty-model-order",
        "pasted-key",
        "extra-key",
        "zero-attempts",
    ],
)
def test_invalid_rejected(data, match):
    with pytest.raises(ConfigError, match=match):
        parse_config(data)


def test_load_wraps_validation_error(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text("bogus: 1\n")
    with pytest.raises(ConfigError):
        load_config(p)


def test_load_bad_yaml_and_non_mapping(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text("a: [unclosed\n")
    with pytest.raises(ConfigError):
        load_config(p)
    p.write_text("- 1\n- 2\n")
    with pytest.raises(ConfigError):
        load_config(p)


def test_missing_file():
    with pytest.raises(ConfigError, match="not found"):
        load_config("/definitely/not/here.yaml")


def test_credentials_present(make_config):
    cfg = make_config()
    env = {
        "OPENROUTER_API_KEY": "x",
        "GEMINI_API_KEY": "   ",
        "GROQ_API_KEY": "",
    }
    assert credentials_present(cfg, env) == frozenset({"openrouter"})
    assert resolve_secret("MISSING", {}) is None
    assert resolve_secret("GEMINI_API_KEY", env) is None


def test_secret_not_in_config_or_logs(make_config, caplog):
    cfg = make_config()
    env = {"OPENROUTER_API_KEY": "SECRET123"}
    with caplog.at_level(logging.DEBUG):
        present = credentials_present(cfg, env)
        resolve_secret("OPENROUTER_API_KEY", env)
    assert "openrouter" in present
    assert "SECRET123" not in repr(cfg)
    assert "SECRET123" not in cfg.model_dump_json()
    assert "SECRET123" not in caplog.text
    assert "SECRET123" not in repr(present)


def _prov(**kw):
    return {"providers": {"a": {"api_key_env": "A_KEY", **kw}}}


def test_provider_extensions_load():
    cfg = parse_config(
        {
            **_prov(
                base_url="https://x.example/v1",
                timeout_s=5,
                models={" m1 ": {"supports_vision": True, "context_window": 10}},
                limits=[
                    {"dimension": "requests_per_day", "limit": 5},
                    {"dimension": "requests_per_day", "limit": 3, "model": " m1 "},
                ],
            ),
            "cooldown": {"rate_limit_default_s": 30, "max_cooldown_s": 600},
        }
    )
    prov = cfg.providers["a"]
    assert prov.base_url == "https://x.example/v1" and prov.timeout_s == 5
    assert "m1" in prov.models and prov.models["m1"].supports_vision is True
    assert prov.limits[1].model == "m1"
    assert cfg.cooldown.rate_limit_default_s == 30
    assert parse_config({}).cooldown.network_failure_threshold == 3


@pytest.mark.parametrize(
    ("data", "match"),
    [
        (_prov(limits=[{"dimension": "spend_usd", "limit": 1}]), "free-only"),
        (_prov(limits=[{"dimension": "requests_per_day", "limit": 0}]), "limit"),
        (_prov(limits=[{"dimension": "requests_per_day", "limit": float("nan")}]), "limit"),
        (_prov(limits=[{"dimension": "requests_per_day", "limit": float("inf")}]), "limit"),
        (
            _prov(
                limits=[
                    {"dimension": "requests_per_day", "limit": 1},
                    {"dimension": "requests_per_day", "limit": 2},
                ]
            ),
            "duplicate limit",
        ),
        (_prov(limits=[{"dimension": "requests_per_week", "limit": 1}]), "dimension"),
        (_prov(limits=[{"dimension": "requests_per_day", "limit": 1, "model": " "}]), "model"),
        (_prov(base_url="ftp://x"), "base_url"),
        (_prov(timeout_s=0), "timeout_s"),
        (_prov(models={" ": {}}), "non-empty"),
        ({"cooldown": {"rate_limit_default_s": 5000}}, "max_cooldown_s"),
        ({"cooldown": {"network_failure_threshold": 0}}, "network_failure_threshold"),
    ],
)
def test_provider_extension_rejected(data, match):
    with pytest.raises(ConfigError, match=match):
        parse_config(data)


def test_example_config_has_cooldown():
    cfg = load_config(ROOT / "config.example.yaml")
    assert cfg.cooldown.max_cooldown_s == 3600


def test_collect_secret_values(make_config):
    from app.config.secrets import collect_secret_values

    cfg = make_config()
    env = {
        "OPENROUTER_API_KEY": "k-or-123456",
        "DISCORD_BOT_TOKEN": "tok-123456",
        "GROQ_API_KEY": " ",
    }
    assert collect_secret_values(cfg, env) == frozenset({"k-or-123456", "tok-123456"})
    assert collect_secret_values(cfg, {}) == frozenset()


def test_opencode_and_orchestrator_defaults():
    cfg = parse_config({})
    oc = cfg.opencode
    assert oc.command == ["opencode"]
    assert oc.managed_server is True and oc.server_port == 4096
    assert oc.server_directory == "data/opencode-server"
    assert oc.server_start_timeout_s == 30 and oc.server_restart_max_backoff_s == 60
    assert oc.env_passthrough == []
    assert oc.run_timeout_s == 1800 and oc.kill_grace_s == 5 and oc.health_timeout_s == 5
    assert oc.max_line_bytes == 1_048_576 and oc.output_tail_chars == 4000
    assert oc.stderr_tail_bytes == 4096 and oc.projects == {}
    assert oc.project_directory("default") == "/workspace"
    assert oc.project_directory("nope") is None
    o = cfg.orchestrator
    assert o.provider_refresh_interval_s == 300 and o.progress_min_interval_s == 10
    assert o.session_idle_close_days == 7 and o.require_tool_calling is False


def test_project_directory_lookup():
    cfg = parse_config({"opencode": {"projects": {"my-proj": "/w/p"}}})
    assert cfg.opencode.project_directory("my-proj") == "/w/p"


@pytest.mark.parametrize(
    "opencode",
    [
        {"provider_map": {"gemini": "google"}},
        {"server_url": "http://example.com:4096"},
        {"server_port": -1},
        {"server_port": 70000},
        {"server_start_timeout_s": 0},
        {"server_restart_max_backoff_s": 601},
        {"projects": {"default": "/x"}},
        {"projects": {"bad name": "/x"}},
        {"command": []},
        {"command": [" "]},
        {"server_url": "ftp://x"},
        {"continue_prompt": "  "},
        {"run_timeout_s": 0},
    ],
)
def test_opencode_invalid(opencode):
    with pytest.raises(ConfigError):
        parse_config({"opencode": opencode})


def test_http_base_url_only_for_localhost():
    prov = {"openrouter": {"api_key_env": "K", "base_url": "http://example.com"}}
    with pytest.raises(ConfigError, match="http:// only for localhost"):
        parse_config({"providers": prov})
    ok = {"openrouter": {"api_key_env": "K", "base_url": "http://localhost:8080"}}
    assert parse_config({"providers": ok}).providers["openrouter"].base_url
    ok = {"openrouter": {"api_key_env": "K", "base_url": "https://example.com"}}
    assert parse_config({"providers": ok})


def test_resolve_secret_strips():
    assert resolve_secret("K", {"K": "  abc "}) == "abc"
    assert resolve_secret("K", {"K": "   "}) is None


def test_provider_map_removed_with_message():
    with pytest.raises(ConfigError, match="opencode.provider_map was removed"):
        parse_config({"opencode": {"provider_map": {"groq": "groq"}}})


def test_server_url_http_must_be_local_but_https_any():
    assert parse_config({"opencode": {"server_url": "http://localhost:4096"}})
    assert parse_config({"opencode": {"server_url": "http://[::1]:4096"}})
    assert parse_config({"opencode": {"server_url": "https://oc.example.com"}})
    with pytest.raises(ConfigError, match="http:// only for localhost"):
        parse_config({"opencode": {"server_url": "http://oc.example.com"}})


def _one_policy(provider, models, **extra):
    return {
        "providers": {provider: {"api_key_env": "K_ENV"}},
        "routing": {
            "provider_order": ["p"],
            "policies": {"p": {"provider": provider, "model_order": models}},
        },
        **extra,
    }


@pytest.mark.parametrize("provider", ["anthropic", "openai", "google", "x"])
def test_unsupported_policy_provider_rejected_as_paid(provider):
    with pytest.raises(ConfigError, match="free-only"):
        parse_config(_one_policy(provider, ["m"]))


def test_openrouter_requires_free_model_but_others_do_not():
    with pytest.raises(ConfigError, match="free"):
        parse_config(_one_policy("openrouter", ["openai/gpt-oss-120b"]))
    assert parse_config(_one_policy("openrouter", ["qwen/qwen3-coder:free"]))
    assert parse_config(_one_policy("groq", ["openai/gpt-oss-120b"]))


@pytest.mark.parametrize("model", ["a{b", "a}b", "{env:SECRET}", "a b", "a	b", "m{file:/x}"])
def test_model_ids_with_braces_or_whitespace_rejected(model):
    with pytest.raises(ConfigError, match="must not contain"):
        parse_config(_one_policy("groq", [model]))


# ---- env_passthrough ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    [
        "OPENCODE_CONFIG",
        "OPENCODE_X",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_PROFILE",
        "AZURE_CLIENT_SECRET",
        "FOO_API_KEY",
        "GOOGLE_APPLICATION_CREDENTIALS",
        "lower",
        "1BAD",
        "",
    ],
)
def test_env_passthrough_rejects_credentialish_names(name):
    with pytest.raises(ConfigError):
        parse_config({"opencode": {"env_passthrough": [name]}})


def test_env_passthrough_accepts_ordinary_names():
    cfg = parse_config({"opencode": {"env_passthrough": ["HTTPS_PROXY", "NODE_OPTIONS"]}})
    assert cfg.opencode.env_passthrough == ["HTTPS_PROXY", "NODE_OPTIONS"]


def test_env_passthrough_must_not_name_provider_key_or_bot_token_env():
    prov = {"groq": {"api_key_env": "MY_GROQ_SECRET"}}
    with pytest.raises(ConfigError, match="MY_GROQ_SECRET"):
        parse_config({"providers": prov, "opencode": {"env_passthrough": ["MY_GROQ_SECRET"]}})
    with pytest.raises(ConfigError, match="MY_BOT"):
        parse_config(
            {
                "discord": {"bot_token_env": "MY_BOT"},
                "opencode": {"env_passthrough": ["MY_BOT"]},
            }
        )


# ---- gateway ------------------------------------------------------------------------------


def test_gateway_defaults_and_bounds():
    g = parse_config({}).gateway
    assert g.port == 0 and g.max_concurrent_requests == 4 and g.small_model is None
    assert g.max_request_bytes == 16_777_216 and g.max_response_bytes == 33_554_432
    assert g.max_error_body_bytes == 65_536
    assert g.upstream_connect_timeout_s == 10 and g.upstream_read_timeout_s == 300
    for bad in (
        {"port": 70000},
        {"max_request_bytes": 10},
        {"max_response_bytes": 10},
        {"max_error_body_bytes": 10},
        {"upstream_read_timeout_s": 0},
        {"max_concurrent_requests": 0},
        {"unknown": 1},
    ):
        with pytest.raises(ConfigError):
            parse_config({"gateway": bad})


def _gw_data(**gateway):
    return {
        "providers": {
            "groq": {"api_key_env": "G_KEY"},
            "gemini": {"api_key_env": "M_KEY"},
        },
        "routing": {
            "provider_order": ["g1", "m1"],
            "policies": {
                "g1": {"provider": "groq", "model_order": ["q1", "q2"]},
                "m1": {"provider": "gemini", "model_order": ["m1"]},
                "unlisted": {"provider": "groq", "model_order": ["q3"]},
            },
        },
        "gateway": gateway,
    }


def test_gateway_models_order_and_exclusions():
    cfg = parse_config(_gw_data())
    assert cfg.gateway_models() == {
        "groq/q1": ("groq", "q1"),
        "groq/q2": ("groq", "q2"),
        "gemini/m1": ("gemini", "m1"),
    }
    assert list(cfg.gateway_models()) == ["groq/q1", "groq/q2", "gemini/m1"]  # not "unlisted"


def test_gateway_models_skip_disabled_policy_and_provider():
    data = _gw_data()
    data["routing"]["policies"]["g1"]["enabled"] = False
    assert list(parse_config(data).gateway_models()) == ["gemini/m1"]
    data = _gw_data()
    data["providers"]["gemini"]["enabled"] = False
    assert list(parse_config(data).gateway_models()) == ["groq/q1", "groq/q2"]


def test_gateway_models_may_be_empty_at_load():
    assert parse_config({}).gateway_models() == {}


def test_gateway_small_model_must_be_allowlisted():
    assert parse_config(_gw_data(small_model="gemini/m1")).gateway.small_model == "gemini/m1"
    for bad in ("groq/q3", "m1", "broke/groq/q1", "openai/gpt-4o"):
        with pytest.raises(ConfigError, match="small_model"):
            parse_config(_gw_data(small_model=bad))
    data = _gw_data(small_model="groq/q1")
    data["routing"]["policies"]["g1"]["enabled"] = False
    with pytest.raises(ConfigError, match="small_model"):
        parse_config(data)
