from __future__ import annotations

import logging
from pathlib import Path

import pytest

from app.config.loader import ConfigError, load_config, parse_config
from app.config.models import RoutingMode
from app.config.secrets import credentials_present, resolve_secret

ROOT = Path(__file__).resolve().parent.parent


def test_example_config_loads():
    cfg = load_config(ROOT / "config.example.yaml")
    assert cfg.routing.provider_order


def test_empty_file_gives_defaults(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text("")
    cfg = load_config(p)
    assert cfg.routing.mode == RoutingMode.FREE_FIRST_NO_PAID
    assert cfg.routing.daily_paid_budget_usd == 0
    assert cfg.routing.max_fallback_attempts == 3
    assert cfg.database.retention_days == 60


def test_confirmation_alias():
    cfg = parse_config({"routing": {"mode": "free-first-paid-after-confirmation"}})
    assert cfg.routing.mode == RoutingMode.FREE_FIRST_PAID_AFTER_APPROVAL


def _pol(provider="a", models=("m",), **kw):
    return {"provider": provider, "cost_class": "FREE", "model_order": list(models), **kw}


def _data(routing, providers=("a", "b")):
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
        ({"routing": {"daily_paid_budget_usd": -1}}, "daily_paid_budget_usd"),
        ({"routing": {"daily_paid_budget_usd": float("inf")}}, "daily_paid_budget_usd"),
        ({"routing": {"monthly_paid_budget_usd": float("inf")}}, "monthly_paid_budget_usd"),
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
        "neg-budget",
        "inf-daily-budget",
        "inf-monthly-budget",
        "zero-attempts",
    ],
)
def test_invalid_rejected(data, match):
    with pytest.raises(ConfigError, match=match):
        parse_config(data)


@pytest.mark.parametrize("key", ["daily_paid_budget_usd", "monthly_paid_budget_usd"])
def test_yaml_inf_budget_rejected(tmp_path, key):
    p = tmp_path / "c.yaml"
    p.write_text(f"routing:\n  {key}: .inf\n")
    with pytest.raises(ConfigError, match=key):
        load_config(p)


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
        "CEREBRAS_API_KEY": "",
        "GROQ_API_KEY": "y",
    }
    assert credentials_present(cfg, env) == frozenset({"openrouter", "groq"})
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
        (_prov(limits=[{"dimension": "spend_usd", "limit": 1}]), "routing budgets"),
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
