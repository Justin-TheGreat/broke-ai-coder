"""Safety-focused additions for slice 3 (auth, fallback, executor, recovery, secrets).

Everything is offline: fake interactions, ScriptedExecutor, and the fake opencode script.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from conftest import NOW, SECRET, secret_env
from fakes.fake_executor import ScriptedExecutor, ok
from test_orchestrator import (
    Recorder,
    build,
    drain,
    failed,
    get,
    started,
    submit,
)

from app.bot.auth import authorize
from app.bot.backend import ControllerBackend
from app.bot.discord_frontend import DiscordFrontend, create_frontend
from app.bot.format import format_event
from app.config.models import AppConfig, DiscordConfig
from app.db.attempts import FallbackAttemptRepository
from app.db.connection import open_database
from app.db.migrations import migrate
from app.db.tasks import TaskRepository
from app.opencode.executor import OpenCodeExecutor
from app.opencode.process import kill_process_tree
from app.orchestrator.orchestrator import Orchestrator
from app.orchestrator.state_machine import TaskStatus
from app.providers.base import ErrorClass, ModelCapability
from app.providers.fake import FakeProviderAdapter
from app.redaction import SecretRedactor
from app.runtime.daemon import AgentController
from app.runtime.interfaces import ExecutionOutcome, ExecutionResult, NullFrontend

FAKE = str(Path(__file__).parent / "fakes" / "fake_opencode.py")


# --------------------------------------------------------------------------
# Discord authorization
# --------------------------------------------------------------------------


def _dcfg(users, guilds, channels):
    return DiscordConfig(
        allowed_user_ids=list(users),
        allowed_guild_ids=list(guilds),
        allowed_channel_ids=list(channels),
    )


@pytest.mark.parametrize("users", [(), (1,)])
@pytest.mark.parametrize("guilds", [(), (2,)])
@pytest.mark.parametrize("channels", [(), (3,)])
def test_auth_allows_only_when_every_list_is_configured_and_matches(users, guilds, channels):
    d = authorize(_dcfg(users, guilds, channels), user_id=1, guild_id=2, channel_id=3)
    assert d.allowed is (bool(users) and bool(guilds) and bool(channels))


def test_auth_ids_are_not_interchangeable_between_lists():
    # user id equals the allowed guild id, guild id equals the allowed channel id, etc.
    cfg = _dcfg(users=(5,), guilds=(6,), channels=(7,))
    assert not authorize(cfg, user_id=6, guild_id=7, channel_id=5).allowed
    assert not authorize(cfg, user_id=5, guild_id=7, channel_id=6).allowed
    assert not authorize(cfg, user_id=5, guild_id=6, channel_id=6).allowed
    assert authorize(cfg, user_id=5, guild_id=6, channel_id=7).allowed


def test_auth_dm_denied_even_with_all_lists_configured_and_channel_matching():
    cfg = _dcfg((1,), (2,), (3,))
    d = authorize(cfg, user_id=1, guild_id=None, channel_id=3)
    assert not d.allowed and d.reason == "dm_not_allowed"
    assert not authorize(_dcfg((), (), ()), user_id=1, guild_id=None, channel_id=None).allowed


def test_default_discord_config_denies_everyone():
    cfg = AppConfig().discord
    assert (
        cfg.allowed_user_ids == []
        or not authorize(cfg, user_id=1, guild_id=2, channel_id=3).allowed
    )
    assert not authorize(cfg, user_id=1, guild_id=2, channel_id=3).allowed


class FakeInteraction:
    def __init__(self, user_id, guild_id, channel_id, log):
        self.user = SimpleNamespace(id=user_id)
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.log = log
        interaction = self

        class Response:
            async def send_message(self, text, **kw):
                interaction.log.append(("send_message", text, kw))

            async def defer(self, **kw):
                interaction.log.append(("defer", kw))

        class Followup:
            async def send(self, text, **kw):
                interaction.log.append(("followup", text, kw))

        self.response = Response()
        self.followup = Followup()


def _frontend_cfg(make_config, tmp_path):
    return make_config(
        database={"path": str(tmp_path / "f.db")},
        opencode={"working_directory": str(tmp_path)},
        discord={
            "allowed_user_ids": [1],
            "allowed_guild_ids": [2],
            "allowed_channel_ids": [3],
        },
    )


def _task_count(c) -> int:
    return c.conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]


@pytest.mark.parametrize(
    ("user", "guild", "channel"),
    [(99, 2, 3), (1, None, 3), (1, 99, 3), (1, 2, 99), (1, 2, None)],
)
async def test_unauthorized_interaction_creates_no_task_and_leaks_nothing(
    make_config, tmp_path, user, guild, channel, caplog
):
    cfg = _frontend_cfg(make_config, tmp_path)
    _c, ex, orch = build(tmp_path, make_config, [ok()])
    fe = DiscordFrontend(cfg, redactor=SecretRedactor([SECRET]), environ={})
    async with started(cfg, orch) as c:
        fe._backend = ControllerBackend(c, cfg)
        log: list = []
        inter = FakeInteraction(user, guild, channel, log)
        prompt = "PROMPT-SENTINEL-do-bad-things"
        with caplog.at_level(logging.DEBUG):
            await fe._handle(inter, "code", {"prompt": prompt, "project": None, "session": None})
            await fe._handle(inter, "status", {})
            await fe._handle(inter, "sessions", {})
            await fe._handle(inter, "cancel", {"task": "abcdef12"})
        await asyncio.sleep(0.05)
        assert _task_count(c) == 0
        assert ex.calls == []
        # Only an ephemeral "Not authorized." is ever sent; nothing is deferred.
        assert [e[0] for e in log] == ["send_message"] * 4
        for _kind, text, kw in log:
            assert text == "Not authorized." and kw == {"ephemeral": True}
        assert all(prompt not in r.getMessage() for r in caplog.records)
        assert any("auth denied" in r.getMessage() for r in caplog.records)


async def test_authorized_interaction_defers_first_then_creates_task(make_config, tmp_path):
    cfg = _frontend_cfg(make_config, tmp_path)
    _c, ex, orch = build(tmp_path, make_config, [ok()])
    fe = DiscordFrontend(cfg, redactor=None, environ={})
    async with started(cfg, orch) as c:
        fe._backend = ControllerBackend(c, cfg)
        log: list = []
        await fe._handle(
            FakeInteraction(1, 2, 3, log),
            "code",
            {"prompt": "hi", "project": None, "session": None},
        )
        assert [e[0] for e in log] == ["defer", "followup"]
        assert log[0][1] == {"ephemeral": False, "thinking": True}
        assert _task_count(c) == 1
        await drain(c)


async def test_backend_not_ready_after_auth_gives_generic_reply(make_config, tmp_path):
    cfg = _frontend_cfg(make_config, tmp_path)
    fe = DiscordFrontend(cfg, redactor=None, environ={})
    log: list = []
    await fe._handle(FakeInteraction(1, 2, 3, log), "status", {})
    assert [e[0] for e in log] == ["defer", "followup"]
    assert log[1][1] == "Controller is not ready."


# --------------------------------------------------------------------------
# Missing token -> NullFrontend, daemon still runs; token never leaks
# --------------------------------------------------------------------------


async def test_missing_token_daemon_starts_and_runs_a_task(make_config, tmp_path, caplog):
    cfg = _frontend_cfg(make_config, tmp_path)
    with caplog.at_level(logging.WARNING):
        fe = create_frontend(cfg, redactor=None, environ={})
    assert isinstance(fe, NullFrontend)
    _c, ex, orch = build(tmp_path, make_config, [ok(session_id="ses_z")])
    c = AgentController(cfg, orchestrator=orch, frontend=fe)
    await c.start()
    try:
        rec = submit(c)
        await drain(c)
        assert get(c, rec.id).status == TaskStatus.SUCCEEDED
    finally:
        await c.stop()


def test_whitespace_only_token_counts_as_missing():
    cfg = AppConfig()
    assert isinstance(
        create_frontend(cfg, redactor=None, environ={"DISCORD_BOT_TOKEN": "   "}), NullFrontend
    )


async def test_start_without_token_creates_no_client_or_task():
    fe = DiscordFrontend(AppConfig(), redactor=None, environ={})
    await fe.start(controller=None)  # type: ignore[arg-type]
    assert fe._task is None and fe._client is None
    await fe.stop()


async def test_login_failure_log_never_contains_token(monkeypatch, caplog):
    import app.bot.discord_frontend as df

    token = "tok-LOGIN-SECRET-0123456789abcdef"
    seen: dict = {}

    async def boom(self, tok, *a, **kw):
        seen["token"] = tok
        raise RuntimeError(f"bad token {tok}")

    monkeypatch.setattr(df._Client, "start", boom)

    class Ctl:
        conn = None

    fe = DiscordFrontend(AppConfig(), redactor=None, environ={"DISCORD_BOT_TOKEN": token})
    with caplog.at_level(logging.DEBUG):
        await fe.start(Ctl())  # type: ignore[arg-type]
        await asyncio.sleep(0.05)
        await fe.stop()
    assert seen["token"] == token  # the stub, not the network, received it
    assert token not in repr(fe) and token not in str(vars(fe))
    for r in caplog.records:
        assert token not in r.getMessage() and token not in str(r.args)
    assert any("RuntimeError" in r.getMessage() for r in caplog.records)


async def test_event_text_is_redacted_before_send():
    fe = DiscordFrontend(AppConfig(), redactor=SecretRedactor([SECRET]), environ={})
    sent = []

    class Chan:
        async def send(self, text, **kw):
            sent.append(text)

    fe._client = SimpleNamespace(get_channel=lambda cid: Chan())
    from app.runtime.daemon import ControllerEvent

    await fe.on_event(
        ControllerEvent(
            "task_finished",
            at=NOW,
            task_id="t",
            fields={
                "channel_id": "5",
                "status": "SUCCEEDED",
                "short_id": "abcdef12",
                "provider": "p",
                "model": "m",
                "output_tail": f"leaked {SECRET} @everyone ```x```",
            },
        )
    )
    assert len(sent) == 1 and SECRET not in sent[0]


# --------------------------------------------------------------------------
# Orchestrator fallback / safety
# --------------------------------------------------------------------------


def _models_of(cfg, provider):
    return [
        m for p in cfg.routing.policies.values() if p.provider == provider for m in p.model_order
    ]


def _allowed_pairs(cfg):
    return {(p.provider, m) for p in cfg.routing.policies.values() for m in p.model_order}


async def test_exhaustion_preserves_order_same_session_and_surfaces_reason(tmp_path, make_config):
    cfg, ex, orch = build(
        tmp_path,
        make_config,
        [failed(session="ses_fake1")],
        routing={"max_fallback_attempts": 10},
    )
    rec_events = Recorder()
    async with started(cfg, orch, rec_events) as c:
        rec = submit(c, "orig")
        await drain(c)
        await c._event_queue.join()
        t = get(c, rec.id)
        assert t.status == TaskStatus.FAILED and t.error_class == "FREE_CAPACITY_EXHAUSTED"
        atts = FallbackAttemptRepository(c.conn).list_for_task(rec.id)
        assert [a.attempt_no for a in atts] == [1, 2, 3, 4, 5]
        pairs = [(a.provider, a.model) for a in atts]
        assert len(set(pairs)) == 5  # no candidate is tried twice
        assert pairs == [(x["provider"], x["model"]) for x in ex.calls]
        # follows the configured policy order
        order = [
            (p.provider, m)
            for pid in cfg.routing.provider_order
            for p in [cfg.routing.policies[pid]]
            for m in p.model_order
        ]
        assert pairs == [o for o in order if o in set(pairs)]
        assert all(a.session_id == t.session_id for a in atts)
        assert {a.error_class for a in atts} == {"RATE_LIMITED"}
        # same OpenCode session on every continuation, original prompt never re-sent
        for call in ex.calls[1:]:
            assert call["session_id"] == "ses_fake1"
            assert call["prompt"] == cfg.opencode.continue_prompt
        # cooldown recorded for each failed model
        active = orch.cooldowns.active(NOW)
        for pair in pairs:
            assert pair in active
        fin = [e for e in rec_events.events if e.name == "task_finished"]
        assert len(fin) == 1
        assert fin[0].fields["error_class"] == "FREE_CAPACITY_EXHAUSTED"
        assert fin[0].fields["status"] == "FAILED" and fin[0].fields["attempts"] == 5
        text = format_event(fin[0], redactor=None)
        assert "No free capacity left" in text and "Nothing was paid for" in text


async def test_retry_after_drives_cooldown_length(tmp_path, make_config):
    cfg, ex, orch = build(tmp_path, make_config, [failed(retry=120), ok()])
    async with started(cfg, orch) as c:
        submit(c)
        await drain(c)
        until = orch.cooldowns.active(NOW)[("openrouter", "openrouter/free")]
        assert (until - NOW).total_seconds() == pytest.approx(120, abs=1)


async def test_nothing_outside_allowlist_or_paid_is_ever_selected(tmp_path, make_config):
    cfg, ex, orch = build(tmp_path, make_config, [failed()], routing={"max_fallback_attempts": 50})
    # Providers also advertise models that are NOT allowlisted (e.g. paid ones).
    extra = {
        name: FakeProviderAdapter(
            name,
            models=[
                ModelCapability(name, m, supports_tool_calling=True, context_window=200_000)
                for m in [*_models_of(cfg, name), "paid-gpt-super", "pro-max"]
            ],
        )
        for name in [p.provider for p in cfg.routing.policies.values()]
    }
    orch2 = Orchestrator(cfg, executor=ex, adapters=extra, environ=secret_env(), clock=lambda: NOW)
    async with started(cfg, orch2) as c:
        submit(c)
        await drain(c)
        allowed = _allowed_pairs(cfg)
        assert ex.calls
        for call in ex.calls:
            assert (call["provider"], call["model"]) in allowed
        rows = c.conn.execute("SELECT DISTINCT cost_class FROM usage_events").fetchall()
        assert [r[0] for r in rows] == ["FREE"]
        assert (
            c.conn.execute("SELECT COUNT(*) FROM tasks WHERE cost_class <> 'FREE'").fetchone()[0]
            == 0
        )


async def test_unknown_exit_class_stops_without_replay(tmp_path, make_config):
    # Non-zero exit with no classifiable error: outcome unknown, never replayed.
    res = failed(ErrorClass.UNKNOWN, session="ses_u", status=None)
    cfg, ex, orch = build(tmp_path, make_config, [res, ok()])
    async with started(cfg, orch) as c:
        rec = submit(c)
        await drain(c)
        t = get(c, rec.id)
        assert t.status == TaskStatus.FAILED and t.error_class == "UNKNOWN"
        assert len(ex.calls) == 1
        statuses = [r[0] for r in c.conn.execute("SELECT status FROM usage_events ORDER BY id")]
        assert statuses[-1] == "unknown_outcome"
        assert len(FallbackAttemptRepository(c.conn).list_for_task(rec.id)) == 1


async def test_tool_activity_without_session_never_replays_and_is_recorded(tmp_path, make_config):
    cfg, ex, orch = build(tmp_path, make_config, [failed(tools=3), ok(), ok()])
    rec_events = Recorder()
    async with started(cfg, orch, rec_events) as c:
        rec = submit(c)
        await drain(c)
        await c._event_queue.join()
        assert len(ex.calls) == 1
        atts = FallbackAttemptRepository(c.conn).list_for_task(rec.id)
        assert len(atts) == 1
        assert get(c, rec.id).error_class == "UNKNOWN"
        fin = [e for e in rec_events.events if e.name == "task_finished"][0]
        assert "Outcome unknown; not replayed" in format_event(fin, redactor=None)


async def test_cancel_between_attempts_stops_before_next_candidate(tmp_path, make_config):
    holder = {}

    async def fail_and_cancel(**kw):
        holder["orch"].request_cancel(kw["task_id"])
        return failed()

    cfg, ex, orch = build(tmp_path, make_config, [fail_and_cancel, ok()])
    holder["orch"] = orch
    async with started(cfg, orch) as c:
        rec = submit(c)
        await drain(c)
        assert get(c, rec.id).status == TaskStatus.CANCELLED
        assert len(ex.calls) == 1


def _finished(rec_events, task_id):
    return [e for e in rec_events.events if e.name == "task_finished" and e.task_id == task_id]


def _assert_failed_unknown(c, rec_events, task_id):
    t = get(c, task_id)
    assert t.status == TaskStatus.FAILED
    assert t.error_class == "UNKNOWN"
    fin = _finished(rec_events, task_id)
    assert len(fin) == 1
    assert fin[0].fields["status"] == "FAILED"
    assert fin[0].fields["error_class"] == "UNKNOWN"
    assert fin[0].fields["session_short"] is not None
    assert "Outcome unknown; not replayed" in format_event(fin[0], redactor=None)


async def test_executor_exception_does_not_wedge_worker(tmp_path, make_config, caplog):
    async def boom(**kw):
        raise RuntimeError("executor exploded secret-msg")

    cfg, ex, orch = build(tmp_path, make_config, [boom, ok()])
    rec_events = Recorder()
    with caplog.at_level(logging.DEBUG):
        async with started(cfg, orch, rec_events) as c:
            first = submit(c, "one")
            second = submit(c, "two")
            await drain(c)
            # The worker survived; the second task ran and finished.
            assert get(c, second.id).status == TaskStatus.SUCCEEDED
            _assert_failed_unknown(c, rec_events, first.id)
            assert len(ex.calls) == 2  # no replay, no fallback for the failed task
            atts = FallbackAttemptRepository(c.conn).list_for_task(first.id)
            assert [(a.status.value, a.error_class) for a in atts] == [("FAILED", "UNKNOWN")]
            assert atts[0].finished_at is not None
            assert orch.running is None
    assert "RuntimeError" in caplog.text
    assert "secret-msg" not in caplog.text and "exploded" not in caplog.text


async def test_routing_exception_marks_task_failed_unknown(tmp_path, make_config, monkeypatch):
    import app.orchestrator.orchestrator as om

    calls = {"n": 0}
    real = om.route_with_state

    def flaky(*a, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise ValueError("router exploded")
        return real(*a, **kw)

    monkeypatch.setattr(om, "route_with_state", flaky)
    cfg, ex, orch = build(tmp_path, make_config, [ok()])
    rec_events = Recorder()
    async with started(cfg, orch, rec_events) as c:
        first = submit(c, "one")
        second = submit(c, "two")
        await drain(c)
        _assert_failed_unknown(c, rec_events, first.id)
        assert get(c, second.id).status == TaskStatus.SUCCEEDED
        assert len(ex.calls) == 1
        assert orch.running is None


async def test_db_exception_marks_task_failed_unknown(tmp_path, make_config, monkeypatch):
    import sqlite3

    real = FallbackAttemptRepository.start
    calls = {"n": 0}

    def flaky(self, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise sqlite3.OperationalError("disk I/O error")
        return real(self, **kw)

    monkeypatch.setattr(FallbackAttemptRepository, "start", flaky)
    cfg, ex, orch = build(tmp_path, make_config, [ok()])
    rec_events = Recorder()
    async with started(cfg, orch, rec_events) as c:
        first = submit(c, "one")
        second = submit(c, "two")
        await drain(c)
        _assert_failed_unknown(c, rec_events, first.id)
        assert get(c, second.id).status == TaskStatus.SUCCEEDED
        assert len(ex.calls) == 1


# --------------------------------------------------------------------------
# Restart recovery
# --------------------------------------------------------------------------


async def test_recovery_marks_every_unfinished_state_and_never_replays(tmp_path, make_config):
    cfg, ex, orch = build(tmp_path, make_config, [ok()])
    conn = open_database(cfg.database.path)
    migrate(conn)
    tasks = TaskRepository(conn)
    ids = {}
    for name, path in {
        "routing": [TaskStatus.ROUTING],
        "running": [TaskStatus.ROUTING, TaskStatus.RUNNING],
        "waiting": [TaskStatus.ROUTING, TaskStatus.RUNNING, TaskStatus.WAITING_APPROVAL],
        "queued": [],
        "done": [TaskStatus.ROUTING, TaskStatus.RUNNING, TaskStatus.SUCCEEDED],
    }.items():
        r = tasks.create(project_id="default", prompt=name, discord_user_id="u", now=NOW)
        for st in path:
            tasks.transition(r.id, st, now=NOW)
        ids[name] = r.id
    conn.close()

    async with started(cfg, orch) as c:
        for name in ("routing", "running", "waiting"):
            t = get(c, ids[name])
            assert t.status == TaskStatus.FAILED and t.error_class == "INTERRUPTED_BY_RESTART"
        q = get(c, ids["queued"])
        assert q.status == TaskStatus.CANCELLED and q.error_class == "INTERRUPTED_BY_RESTART"
        assert get(c, ids["done"]).status == TaskStatus.SUCCEEDED
        await asyncio.sleep(0.1)
        assert ex.calls == []  # nothing replayed
        assert c.conn.execute("SELECT COUNT(*) FROM fallback_attempts").fetchone()[0] == 0
        assert c.conn.execute("SELECT COUNT(*) FROM usage_events").fetchone()[0] == 0

    # second start: still nothing replayed, nothing re-recovered
    async with started(cfg, orch) as c:
        await asyncio.sleep(0.1)
        assert ex.calls == []
        assert orch.recover_interrupted() == 0


# --------------------------------------------------------------------------
# Real executor + orchestrator: no blind replay, secrets
# --------------------------------------------------------------------------


def _real_orch(tmp_path, make_config, scenarios, *, redactor=None, env=None, opencode=None):
    cfg = make_config(
        database={"path": str(tmp_path / "o.db")},
        opencode={
            "working_directory": str(tmp_path),
            "command": [sys.executable, FAKE],
            **(opencode or {}),
        },
        routing={"max_fallback_attempts": 5},
    )
    extra = {
        "FAKE_OC_SCENARIOS": scenarios,
        "FAKE_OC_ARGV_FILE": str(tmp_path / "argv.txt"),
        "FAKE_OC_COUNTER_FILE": str(tmp_path / "counter.txt"),
        "FAKE_OC_HEARTBEAT": str(tmp_path / "hb.txt"),
        **(env or {}),
    }
    from app.opencode.config_check import allowed_small_models

    safe = sorted(allowed_small_models(cfg))[0]

    def handler(req):
        if req.url.path == "/config":
            return httpx.Response(200, json={"small_model": safe})
        return httpx.Response(200, json={"healthy": True})

    transport = httpx.MockTransport(handler)
    executor = OpenCodeExecutor(
        cfg.opencode, redactor=redactor, transport=transport, extra_env=extra
    )
    adapters = {
        pol.provider: FakeProviderAdapter(
            pol.provider,
            models=[
                ModelCapability(pol.provider, m, supports_tool_calling=True, context_window=200_000)
                for m in pol.model_order
            ],
        )
        for pol in cfg.routing.policies.values()
    }
    return cfg, Orchestrator(cfg, executor=executor, adapters=adapters, environ=secret_env())


def _argv(tmp_path):
    p = tmp_path / "argv.txt"
    return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []


async def _real_run(cfg, orch, prompt="the original prompt"):
    async with started(cfg, orch) as c:
        rec = submit(c, prompt)
        await asyncio.wait_for(c._task_queue.join(), 60)
        return get(c, rec.id)


async def test_real_tool_activity_without_session_is_not_replayed(tmp_path, make_config):
    cfg, orch = _real_orch(tmp_path, make_config, "tool_no_session,success")
    t = await _real_run(cfg, orch)
    assert t.status == TaskStatus.FAILED and t.error_class == "UNKNOWN"
    assert len(_argv(tmp_path)) == 1


async def test_real_429_without_session_resends_original_prompt_fresh(tmp_path, make_config):
    cfg, orch = _real_orch(tmp_path, make_config, "no_session_429,success")
    t = await _real_run(cfg, orch, "the original prompt")
    assert t.status == TaskStatus.SUCCEEDED
    lines = _argv(tmp_path)
    assert len(lines) == 2
    assert all(x[-1] == "the original prompt" for x in lines)
    assert all("--session" not in x for x in lines)


async def test_real_timeout_is_unknown_outcome_and_not_replayed(tmp_path, make_config):
    cfg, orch = _real_orch(
        tmp_path, make_config, "hang,success", opencode={"run_timeout_s": 1, "kill_grace_s": 1}
    )
    t = await _real_run(cfg, orch)
    assert t.status == TaskStatus.FAILED and t.error_class == "TIMEOUT_UNKNOWN_OUTCOME"
    assert len(_argv(tmp_path)) == 1


async def test_secrets_never_reach_db_events_or_logs(tmp_path, make_config, caplog):
    key = secret_env()["OPENROUTER_API_KEY"]
    cfg, orch = _real_orch(
        tmp_path,
        make_config,
        "secret_echo",
        redactor=SecretRedactor([key, *secret_env().values()]),
        env={"FAKE_OC_SECRET": key},
    )
    rec_events = Recorder()
    with caplog.at_level(logging.DEBUG):
        async with started(cfg, orch, rec_events) as c:
            submit(c, "do it")
            await asyncio.wait_for(c._task_queue.join(), 60)
            await c._event_queue.join()
            dump = "\n".join(c.conn.iterdump())
    assert key not in dump
    for e in rec_events.events:
        assert key not in repr(e)
    for r in caplog.records:
        assert key not in r.getMessage()
    assert any(e.name == "task_finished" for e in rec_events.events)


async def test_stderr_secret_redacted_from_error_message(tmp_path):
    ex = _exec(
        tmp_path, "stderr_secret", redactor=SecretRedactor([SECRET]), env={"FAKE_OC_SECRET": SECRET}
    )
    r = await _run(ex, tmp_path)
    assert r.outcome == ExecutionOutcome.FAILED and r.exit_code == 2
    assert SECRET not in (r.error_message or "") and "[REDACTED]" in (r.error_message or "")


# --------------------------------------------------------------------------
# Executor additions
# --------------------------------------------------------------------------


def _exec(tmp_path, scenarios="success", *, redactor=None, env=None, **cfg):
    from app.config.models import OpenCodeConfig

    extra = {
        "FAKE_OC_SCENARIOS": scenarios,
        "FAKE_OC_ARGV_FILE": str(tmp_path / "argv.txt"),
        "FAKE_OC_COUNTER_FILE": str(tmp_path / "counter.txt"),
        "FAKE_OC_HEARTBEAT": str(tmp_path / "hb.txt"),
        **(env or {}),
    }
    config = OpenCodeConfig(command=[sys.executable, FAKE], **cfg)
    return OpenCodeExecutor(
        config,
        redactor=redactor,
        extra_env=extra,
        transport=httpx.MockTransport(lambda r: httpx.Response(200)),
    )


async def _run(ex, tmp_path, *, prompt="do it", cancel=None):
    return await asyncio.wait_for(
        ex.run(
            task_id="t1",
            provider="openrouter",
            model="openrouter/free",
            prompt=prompt,
            session_id=None,
            project_dir=str(tmp_path),
            cancel_event=cancel or asyncio.Event(),
        ),
        60,
    )


async def _hb_stops(tmp_path):
    hb = tmp_path / "hb.txt"
    for _ in range(100):
        if hb.exists() and hb.stat().st_size > 0:
            break
        await asyncio.sleep(0.1)
    assert hb.exists() and hb.stat().st_size > 0


async def _assert_hb_frozen(tmp_path):
    hb = tmp_path / "hb.txt"
    await asyncio.sleep(1)
    size = hb.stat().st_size
    await asyncio.sleep(1)
    assert hb.stat().st_size == size


async def test_timeout_kills_whole_process_tree(tmp_path):
    ex = _exec(tmp_path, "spawn_child", run_timeout_s=2, kill_grace_s=1)
    r = await _run(ex, tmp_path)
    assert r.outcome == ExecutionOutcome.TIMED_OUT
    assert r.error_class == ErrorClass.TIMEOUT_UNKNOWN_OUTCOME
    await _hb_stops(tmp_path)
    await _assert_hb_frozen(tmp_path)


async def test_cancel_set_before_run_still_kills_tree(tmp_path):
    ex = _exec(tmp_path, "spawn_child", kill_grace_s=1)
    cancel = asyncio.Event()
    cancel.set()
    r = await _run(ex, tmp_path, cancel=cancel)
    assert r.outcome == ExecutionOutcome.CANCELLED
    if (tmp_path / "hb.txt").exists():
        await _assert_hb_frozen(tmp_path)


async def test_stderr_is_bounded_and_keeps_tail(tmp_path):
    ex = _exec(tmp_path, "stderr_flood", stderr_tail_bytes=512)
    r = await _run(ex, tmp_path)
    assert r.outcome == ExecutionOutcome.FAILED and r.exit_code == 2
    assert len(r.error_message or "") <= 300
    assert "final-marker" in (r.error_message or "")


async def test_shell_metacharacters_in_prompt_are_passed_verbatim(tmp_path):
    marker = tmp_path / "pwned.txt"
    prompt = (
        f'& echo pwned > "{marker}" ; $(touch {marker}) `touch {marker}` | whoami "quoted" \'x\''
    )
    ex = _exec(tmp_path)
    r = await _run(ex, tmp_path, prompt=prompt)
    assert r.outcome == ExecutionOutcome.SUCCEEDED
    assert _argv(tmp_path)[0][-1] == prompt
    assert not marker.exists()


async def test_oversized_text_flood_keeps_memory_bounded_and_counts_nothing_extra(tmp_path):
    ex = _exec(tmp_path, "flood", output_tail_chars=200)
    r = await _run(ex, tmp_path)
    assert len(r.output_tail) <= 200 and r.output_truncated and r.tool_events == 0


@pytest.mark.skipif(sys.platform != "win32", reason="Windows-only launcher check")
async def test_bat_launcher_refused_and_not_spawned(tmp_path, monkeypatch):
    from app.config.models import OpenCodeConfig

    marker = tmp_path / "ran.txt"
    bat = tmp_path / "oc.bat"
    bat.write_text(f"@echo off\r\necho x> {marker}\r\n")
    monkeypatch.setattr("app.opencode.executor.shutil.which", lambda _c: str(bat))
    ex = OpenCodeExecutor(OpenCodeConfig(command=["opencode"]))
    r = await _run(ex, tmp_path)
    assert r.outcome == ExecutionOutcome.EXECUTOR_UNAVAILABLE
    assert not marker.exists()


async def test_kill_process_tree_on_exited_process_never_raises(tmp_path):
    proc = await asyncio.create_subprocess_exec(sys.executable, "-c", "pass")
    await proc.wait()
    await kill_process_tree(proc, grace_s=0.2)
    assert proc.returncode == 0


async def test_on_event_callback_exception_does_not_break_run(tmp_path):
    def bad(_e):
        raise ValueError("cb")

    ex = _exec(tmp_path)
    r = await asyncio.wait_for(
        ex.run(
            task_id="t",
            provider="gemini",
            model="m",
            prompt="x",
            session_id=None,
            project_dir=str(tmp_path),
            cancel_event=asyncio.Event(),
            on_event=bad,
        ),
        60,
    )
    assert r.outcome == ExecutionOutcome.SUCCEEDED


def test_scripted_result_helpers_are_consistent():
    # guards the helpers this module relies on
    assert isinstance(ScriptedExecutor([ok()]), ScriptedExecutor)
    assert isinstance(ok(), ExecutionResult)
