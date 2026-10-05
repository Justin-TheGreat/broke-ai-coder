from __future__ import annotations

import json

from app.opencode.auth_store import (
    auth_store_path,
    auth_store_problem,
    stored_credential_providers,
)


def env(tmp_path):
    return {"XDG_DATA_HOME": str(tmp_path)}


def write(tmp_path, text):
    d = tmp_path / "opencode"
    d.mkdir(parents=True, exist_ok=True)
    (d / "auth.json").write_text(text, encoding="utf-8")


def test_path_uses_xdg_data_home_else_home(tmp_path):
    assert auth_store_path(env(tmp_path)) == tmp_path / "opencode" / "auth.json"
    default = auth_store_path({})
    assert default.parts[-4:] == (".local", "share", "opencode", "auth.json")


def test_missing_file_is_empty_and_no_problem(tmp_path):
    assert stored_credential_providers(env(tmp_path)) == []
    assert auth_store_problem(env(tmp_path)) is None


def test_empty_object_is_no_problem(tmp_path):
    write(tmp_path, "{}")
    assert stored_credential_providers(env(tmp_path)) == []
    assert auth_store_problem(env(tmp_path)) is None


def test_keys_sorted_and_values_never_returned(tmp_path):
    write(tmp_path, json.dumps({"openai": {"key": "sk-SECRET-1"}, "anthropic": {"key": "sk-2"}}))
    assert stored_credential_providers(env(tmp_path)) == ["anthropic", "openai"]
    msg = auth_store_problem(env(tmp_path))
    assert msg.startswith("OpenCode has stored provider credentials for: anthropic, openai. ")
    assert "rm -f " in msg and "auth.json" in msg  # delete the store; never `auth logout`
    assert msg.endswith("OpenCode must hold no provider keys.")
    assert "SECRET" not in msg and "sk-2" not in msg


def test_invalid_json_non_object_and_directory_are_unknown(tmp_path):
    write(tmp_path, "{nope")
    assert stored_credential_providers(env(tmp_path)) is None
    msg = auth_store_problem(env(tmp_path))
    assert msg == f"OpenCode credential store unreadable: {tmp_path / 'opencode' / 'auth.json'}"
    write(tmp_path, "[1]")
    assert stored_credential_providers(env(tmp_path)) is None
    (tmp_path / "opencode" / "auth.json").unlink()
    (tmp_path / "opencode" / "auth.json").mkdir()
    assert stored_credential_providers(env(tmp_path)) is None


def test_keys_are_capped_with_more_marker(tmp_path):
    write(tmp_path, json.dumps({f"p{i:02d}": {} for i in range(13)}))
    out = stored_credential_providers(env(tmp_path))
    assert out is not None and len(out) == 11
    assert out[:10] == [f"p{i:02d}" for i in range(10)] and out[-1] == "+3 more"


def test_keys_are_sanitized_and_truncated(tmp_path):
    write(tmp_path, json.dumps({"bad key/é": {}, "x" * 100: {}}))
    out = stored_credential_providers(env(tmp_path))
    assert out == ["bad?key??", "x" * 40]
