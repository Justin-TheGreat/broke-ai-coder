"""Fix A (allowlist scope), Fix B (on-disk project config, B13) and F18 (all model keys)."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import httpx
import pytest
from conftest import NOW, _base_config_dict, secret_env

from app.config.loader import ConfigError, parse_config
from app.db.tasks import TaskRepository
from app.opencode.config_check import (
    allowed_small_models,
    check_project_config,
    check_small_model,
    strip_jsonc,
)
from app.opencode.executor import OpenCodeExecutor
from app.orchestrator.orchestrator import Orchestrator
from app.orchestrator.state_machine import TaskStatus
from app.providers.base import ModelCapability
from app.providers.fake import FakeProviderAdapter
from app.runtime.daemon import AgentController, TaskSubmission
from app.runtime.interfaces import SmallModelCheck

OK = "groq/q-x"
ALLOWED = frozenset({OK, "openrouter/openrouter/free"})
FAKE = str(Path(__file__).parent / "fakes" / "fake_opencode.py")


# ---- Fix A -----------------------------------------------------------------------------


def _anthropic_data():
    data = _base_config_dict()
    data["providers"]["anthropic"] = {"enabled": True, "api_key_env": "ANTHROPIC_API_KEY"}
    data["routing"]["policies"]["anthropic-x"] = {
        "provider": "anthropic",
        "model_order": ["claude-opus-4"],
    }
    data["routing"]["provider_order"].append("anthropic-x")
    return data


def test_unmapped_provider_rejected_at_load():
    # The exact repro: provider "anthropic" / model "claude-opus-4" must not load at all.
    with pytest.raises(ConfigError, match="not mapped"):
        parse_config(_anthropic_data())


def test_anthropic_never_reaches_allowlist(make_config):
    assert "anthropic/claude-opus-4" not in allowed_small_models(make_config())


def test_allowlist_excludes_disabled_policy_and_provider(make_config):
    full = allowed_small_models(make_config())
    assert "groq/q-x" in full and "google/g-3.8" in full
    cfg = make_config(providers={"groq": {"enabled": False, "api_key_env": "GROQ_API_KEY"}})
    out = allowed_small_models(cfg)
    assert not any(m.startswith("groq/") for m in out) and "google/g-3.8" in out
    pol = _base_config_dict()["routing"]["policies"]
    pol["gemini-free"]["enabled"] = False
    out2 = allowed_small_models(make_config(routing={"policies": pol}))
    assert not any(m.startswith("google/") for m in out2) and "groq/q-x" in out2


# ---- JSONC -----------------------------------------------------------------------------


def test_jsonc_comments_and_trailing_commas():
    text = (
        "{\n"
        '  // line comment with "quote and /* not a block\n'
        '  "model": "groq/q-x", /* block */\n'
        '  "url": "http://x//y", "esc": "a\\"//b",\n'
        '  "list": [1, 2,],\n'
        "}"
    )
    assert json.loads(strip_jsonc(text)) == {
        "model": "groq/q-x",
        "url": "http://x//y",
        "esc": 'a"//b',
        "list": [1, 2],
    }


# ---- on-disk project check -------------------------------------------------------------


def write(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(data if isinstance(data, str) else json.dumps(data), encoding="utf-8")


def check(d: Path):
    return check_project_config(str(d), ALLOWED)


def test_empty_project_ok(tmp_path):
    assert check(tmp_path) == (SmallModelCheck.OK, "")


def test_project_opencode_json_paid_small_model_unsafe(tmp_path):
    write(tmp_path / "opencode.json", {"small_model": "anthropic/claude-opus-4"})
    status, detail = check(tmp_path)
    assert status == SmallModelCheck.UNSAFE
    assert "opencode.json" in detail and "small_model" in detail
    assert "claude" not in detail  # the value is never echoed


def test_project_allowlisted_values_ok(tmp_path):
    write(
        tmp_path / "opencode.json",
        {"model": OK, "small_model": OK, "agent": {"a": {"model": OK, "x": 1}}, "mode": {}},
    )
    assert check(tmp_path)[0] == SmallModelCheck.OK


def test_project_a_ok_does_not_vouch_for_project_b(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    write(a / "opencode.json", {"small_model": OK})
    write(b / "opencode.json", {"small_model": "openai/gpt-4o"})
    assert check(a)[0] == SmallModelCheck.OK
    assert check(b)[0] == SmallModelCheck.UNSAFE
    assert check(a)[0] == SmallModelCheck.OK


@pytest.mark.parametrize(
    ("rel", "data", "key"),
    [
        (".opencode/x.json", {"agent": {"build": {"model": "openai/gpt-4o"}}}, "agent.build.model"),
        (".opencode/y.jsonc", {"mode": {"plan": {"model": "openai/gpt-4o"}}}, "mode.plan.model"),
        ("opencode.jsonc", {"model": "openai/gpt-4o"}, "model"),
        ("opencode.json", {"model": 7}, "model"),
        ("opencode.json", {"model": None}, "model"),
        ("opencode.json", {"agent": {"a": {"model": ["x"]}}}, "agent.a.model"),
        ("opencode.json", {"agent": "oops"}, "agent"),
        ("opencode.json", {"mode": {"a": 1}}, "mode.a"),
    ],
)
def test_bad_model_keys_unsafe(tmp_path, rel, data, key):
    write(tmp_path / rel, data)
    status, detail = check(tmp_path)
    assert status == SmallModelCheck.UNSAFE and key in detail and rel.split("/")[-1] in detail


def test_parent_dirs_up_to_git_root_checked_not_above(tmp_path):
    root = tmp_path / "repo"
    (root / ".git").mkdir(parents=True)
    proj = root / "pkg" / "sub"
    proj.mkdir(parents=True)
    write(tmp_path / "opencode.json", {"model": "openai/gpt-4o"})  # above the git root
    assert check(proj)[0] == SmallModelCheck.OK
    write(root / "pkg" / "opencode.json", {"small_model": "openai/gpt-4o"})
    assert check(proj)[0] == SmallModelCheck.UNSAFE
    (root / "pkg" / "opencode.json").unlink()
    write(root / "opencode.json", {"small_model": "openai/gpt-4o"})  # the git root itself
    assert check(proj)[0] == SmallModelCheck.UNSAFE


def test_without_git_only_project_dir_checked(tmp_path):
    proj = tmp_path / "p"
    proj.mkdir()
    write(tmp_path / "opencode.json", {"model": "openai/gpt-4o"})
    assert check(proj)[0] == SmallModelCheck.OK


def test_jsonc_file_with_comments_parses(tmp_path):
    write(
        tmp_path / "opencode.jsonc",
        '{\n // c\n "small_model": "groq/q-x", /* b */\n'
        ' "agent": {"a": {"model": "groq/q-x",},},\n}',
    )
    assert check(tmp_path)[0] == SmallModelCheck.OK


@pytest.mark.parametrize("content", ["{not json", "", "[1]", '{"model": "groq/q-x"', "\x00\x01"])
def test_unparseable_unsafe(tmp_path, content):
    write(tmp_path / "opencode.json", content)
    assert check(tmp_path)[0] == SmallModelCheck.UNSAFE


def test_unreadable_non_file_unsafe(tmp_path):
    (tmp_path / "opencode.json").mkdir()
    assert check(tmp_path)[0] == SmallModelCheck.UNSAFE


# ---- F18: /config response keys ---------------------------------------------------------


async def probe(body):
    return await check_small_model(
        "http://127.0.0.1:4096",
        ALLOWED,
        timeout_s=1,
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json=body)),
    )


@pytest.mark.parametrize(
    "extra",
    [
        {"model": "openai/gpt-4o"},
        {"model": 3},
        {"agent": {"a": {"model": "openai/gpt-4o"}}},
        {"mode": {"a": {"model": "openai/gpt-4o"}}},
        {"agent": []},
    ],
)
async def test_config_response_other_model_keys_unsafe(extra):
    assert await probe({"small_model": OK, **extra}) == SmallModelCheck.UNSAFE


async def test_config_response_other_keys_optional_and_small_required():
    assert await probe({"small_model": OK}) == SmallModelCheck.OK
    ok = {"small_model": OK, "model": OK, "agent": {"a": {"model": OK}, "b": {}}}
    assert await probe(ok) == SmallModelCheck.OK
    assert await probe({"model": OK}) == SmallModelCheck.UNSAFE


# ---- child env ---------------------------------------------------------------------------


def test_child_env_strips_config_injection_vars(monkeypatch, make_config):
    monkeypatch.setenv("OPENCODE_CONFIG", "/x/oc.json")
    monkeypatch.setenv("OPENCODE_CONFIG_CONTENT", '{"model":"openai/gpt-4o"}')
    monkeypatch.setenv("GROQ_API_KEY", "keep")
    ex = OpenCodeExecutor(
        make_config().opencode,
        extra_env={"OPENCODE_CONFIG_CONTENT": "{}", "OPENCODE_CONFIG": "y"},
    )
    env = ex._child_env()
    assert "OPENCODE_CONFIG" not in env and "OPENCODE_CONFIG_CONTENT" not in env
    assert env["GROQ_API_KEY"] == "keep"


# ---- orchestrator: executor never spawned on UNSAFE -------------------------------------


async def _run(tmp_path, make_config, project_files):
    (tmp_path / "proj").mkdir(exist_ok=True)
    for rel, data in project_files.items():
        write(tmp_path / "proj" / rel, data)
    cfg = make_config(
        database={"path": str(tmp_path / "o.db")},
        opencode={"working_directory": str(tmp_path / "proj"), "command": [sys.executable, FAKE]},
    )
    argv = tmp_path / "argv.txt"
    safe = sorted(allowed_small_models(cfg))[0]

    def handler(req):
        if req.url.path == "/config":
            return httpx.Response(200, json={"small_model": safe})
        return httpx.Response(200, json={"healthy": True})

    executor = OpenCodeExecutor(
        cfg.opencode,
        transport=httpx.MockTransport(handler),
        extra_env={"FAKE_OC_SCENARIOS": "success", "FAKE_OC_ARGV_FILE": str(argv)},
    )
    adapters = {
        p.provider: FakeProviderAdapter(
            p.provider,
            models=[
                ModelCapability(p.provider, m, supports_tool_calling=True, context_window=200_000)
                for m in p.model_order
            ],
        )
        for p in cfg.routing.policies.values()
    }
    orch = Orchestrator(
        cfg, executor=executor, adapters=adapters, environ=secret_env(), clock=lambda: NOW
    )
    c = AgentController(cfg, orchestrator=orch)
    await c.start()
    try:
        rec = c.enqueue_task(
            TaskSubmission(
                project_id="default", prompt="go", discord_user_id="1", discord_channel_id="2"
            )
        )
        await asyncio.wait_for(c._task_queue.join(), 30)
        return TaskRepository(c.conn).get(rec.id), argv.exists()
    finally:
        await c.stop()


async def test_orchestrator_unsafe_project_config_never_spawns(tmp_path, make_config):
    t, spawned = await _run(
        tmp_path, make_config, {"opencode.json": {"small_model": "openai/gpt-4o"}}
    )
    assert t.status == TaskStatus.FAILED and t.error_class == "OPENCODE_UNSAFE_CONFIG"
    assert not spawned


async def test_orchestrator_clean_project_runs(tmp_path, make_config):
    t, spawned = await _run(tmp_path, make_config, {})
    assert t.status == TaskStatus.SUCCEEDED and spawned


# ---- extra coverage: URLs in JSONC, no side effects, cross-task, env token ---------------


def test_jsonc_url_values_are_not_comments(tmp_path):
    text = (
        "{\n"
        '  "$schema": "https://opencode.ai/config.json", // trailing comment\n'
        '  "provider": {"x": {"options": {"baseURL": "https://api.example.com/v1//z"}}},\n'
        f'  "small_model": "{OK}"\n'
        "}"
    )
    parsed = json.loads(strip_jsonc(text))
    assert parsed["$schema"] == "https://opencode.ai/config.json"
    assert parsed["provider"]["x"]["options"]["baseURL"] == "https://api.example.com/v1//z"
    write(tmp_path / "opencode.jsonc", text)
    assert check(tmp_path)[0] == SmallModelCheck.OK


def test_jsonc_url_config_with_bad_model_still_unsafe(tmp_path):
    # A URL before the bad key must not swallow the rest of the line/file as a comment.
    write(
        tmp_path / "opencode.jsonc",
        '{"$schema": "https://opencode.ai/config.json", "small_model": "openai/gpt-4o"}',
    )
    status, detail = check(tmp_path)
    assert status == SmallModelCheck.UNSAFE and "small_model" in detail
    assert "gpt-4o" not in detail


def test_detail_names_file_and_key_path_not_value(tmp_path):
    write(tmp_path / ".opencode" / "z.json", {"agent": {"b": {"model": "secret-paid/zzz"}}})
    status, detail = check(tmp_path)
    assert status == SmallModelCheck.UNSAFE
    assert "z.json" in detail and "agent.b.model" in detail and "secret-paid" not in detail


def test_child_env_strips_bot_token_keeps_provider_keys(monkeypatch, make_config):
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "tok")
    monkeypatch.setenv("GROQ_API_KEY", "keep")
    ex = OpenCodeExecutor(make_config().opencode, bot_token_env="DISCORD_BOT_TOKEN")
    env = ex._child_env()
    assert "DISCORD_BOT_TOKEN" not in env and env["GROQ_API_KEY"] == "keep"


async def test_unsafe_project_config_no_attempt_no_cooldown(tmp_path, make_config):
    t, spawned, n_attempts, cooldowns, detail_ok = await _run_full(
        tmp_path, make_config, [{"opencode.json": {"model": "openai/gpt-4o"}}]
    )
    assert t[0].status == TaskStatus.FAILED and t[0].error_class == "OPENCODE_UNSAFE_CONFIG"
    assert not spawned and n_attempts == 0 and not cooldowns


async def test_agent_written_config_between_tasks_blocks_second(tmp_path, make_config):
    t, spawned, _, _, _ = await _run_full(
        tmp_path,
        make_config,
        [{}, {"opencode.json": {"small_model": "openai/gpt-4o"}}],
    )
    assert t[0].status == TaskStatus.SUCCEEDED
    assert t[1].status == TaskStatus.FAILED and t[1].error_class == "OPENCODE_UNSAFE_CONFIG"


async def _run_full(tmp_path, make_config, steps):
    """Run one task per step; each step's files are written into the project before its task."""
    proj = tmp_path / "proj"
    proj.mkdir(exist_ok=True)
    cfg = make_config(
        database={"path": str(tmp_path / "o.db")},
        opencode={"working_directory": str(proj), "command": [sys.executable, FAKE]},
    )
    argv = tmp_path / "argv.txt"
    safe = sorted(allowed_small_models(cfg))[0]

    def handler(req):
        if req.url.path == "/config":
            return httpx.Response(200, json={"small_model": safe})
        return httpx.Response(200, json={"healthy": True})

    executor = OpenCodeExecutor(
        cfg.opencode,
        transport=httpx.MockTransport(handler),
        extra_env={"FAKE_OC_SCENARIOS": "success", "FAKE_OC_ARGV_FILE": str(argv)},
    )
    adapters = {
        p.provider: FakeProviderAdapter(
            p.provider,
            models=[
                ModelCapability(p.provider, m, supports_tool_calling=True, context_window=200_000)
                for m in p.model_order
            ],
        )
        for p in cfg.routing.policies.values()
    }
    orch = Orchestrator(
        cfg, executor=executor, adapters=adapters, environ=secret_env(), clock=lambda: NOW
    )
    c = AgentController(cfg, orchestrator=orch)
    await c.start()
    tasks = []
    try:
        for files in steps:
            for rel, data in files.items():
                write(proj / rel, data)
            rec = c.enqueue_task(
                TaskSubmission(
                    project_id="default", prompt="go", discord_user_id="1", discord_channel_id="2"
                )
            )
            await asyncio.wait_for(c._task_queue.join(), 30)
            tasks.append(TaskRepository(c.conn).get(rec.id))
        n = c.conn.execute("SELECT COUNT(*) FROM fallback_attempts").fetchone()[0]
        return tasks, argv.exists(), n, orch.cooldowns.active(NOW), True
    finally:
        await c.stop()


def test_allowlist_excludes_provider_mapped_to_paid_even_if_config_mutated(make_config):
    # Defense in depth: bypass load validation by mutating the map after load.
    cfg = make_config()
    cfg.opencode.provider_map["groq"] = "anthropic"
    out = allowed_small_models(cfg)
    assert not any(m.startswith(("anthropic/", "groq/")) for m in out)
    del cfg.opencode.provider_map["groq"]
    assert not any(m.startswith("groq/") for m in allowed_small_models(cfg))
