from __future__ import annotations

from app.opencode.env import BASE_ENV_NAMES, build_opencode_env

ENVIRON = {
    "PATH": "/usr/bin",
    "HOME": "/home/u",
    "LC_ALL": "C.UTF-8",
    "LC_CTYPE": "x",
    "OPENROUTER_API_KEY": "sk-or-1",
    "GEMINI_API_KEY": "g-1",
    "GROQ_API_KEY": "gq-1",
    "GOOGLE_GENERATIVE_AI_API_KEY": "gg-1",
    "DISCORD_BOT_TOKEN": "tok",
    "OPENCODE_CONFIG": "/x.json",
    "OPENCODE_CONFIG_DIR": "/x",
    "OPENCODE_CONFIG_CONTENT": "{}",
    "FOO_API_KEY": "foo",
    "UNKNOWN_VAR": "u",
    "MY_PROXY": "proxy",
}


def test_secrets_and_unknown_variables_are_absent():
    env = build_opencode_env(ENVIRON, passthrough=[], extra={})
    for name in (
        "OPENROUTER_API_KEY",
        "GEMINI_API_KEY",
        "GROQ_API_KEY",
        "GOOGLE_GENERATIVE_AI_API_KEY",
        "DISCORD_BOT_TOKEN",
        "OPENCODE_CONFIG",
        "OPENCODE_CONFIG_DIR",
        "OPENCODE_CONFIG_CONTENT",
        "FOO_API_KEY",
        "UNKNOWN_VAR",
        "MY_PROXY",
    ):
        assert name not in env, name


def test_base_names_lc_and_passthrough_present():
    env = build_opencode_env(ENVIRON, passthrough=["MY_PROXY", "NOT_SET"], extra={})
    assert env["PATH"] == "/usr/bin" and env["HOME"] == "/home/u"
    assert env["LC_ALL"] == "C.UTF-8" and env["LC_CTYPE"] == "x"
    assert env["MY_PROXY"] == "proxy"
    assert "NOT_SET" not in env


def test_extra_overrides_and_adds():
    env = build_opencode_env(
        ENVIRON, passthrough=["MY_PROXY"], extra={"PATH": "/override", "NEW": "1"}
    )
    assert env["PATH"] == "/override" and env["NEW"] == "1" and env["MY_PROXY"] == "proxy"


def test_base_names_contain_no_credential_like_names():
    assert not any(n.endswith(("_KEY", "_TOKEN", "_SECRET")) for n in BASE_ENV_NAMES)
    assert not any(n.startswith(("OPENCODE_", "AWS_", "AZURE_")) for n in BASE_ENV_NAMES)
