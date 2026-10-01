from __future__ import annotations

from conftest import NOW, secret_env
from fakes.fake_executor import ScriptedExecutor, ok

from app.bot.backend import ControllerBackend
from app.bot.commands import CommandContext, SubmitStatus
from app.db.sessions import SessionRepository
from app.orchestrator.orchestrator import CancelResult, Orchestrator
from app.runtime.daemon import AgentController

CTX = CommandContext(1, 2, 3)


async def make(tmp_path, make_config, **overrides):
    cfg = make_config(
        database={"path": str(tmp_path / "b.db")},
        opencode={"working_directory": str(tmp_path), "projects": {"other": str(tmp_path)}},
        **overrides,
    )
    orch = Orchestrator(
        cfg,
        executor=ScriptedExecutor([ok()]),
        adapters={},
        environ=secret_env(),
        clock=lambda: NOW,
    )
    c = AgentController(cfg, orchestrator=orch)
    await c.start()
    return cfg, c, ControllerBackend(c, cfg)


async def test_submit_validation_and_queue(tmp_path, make_config):
    cfg, c, b = await make(tmp_path, make_config)
    try:
        assert b.submit_code(prompt="  ", project=None, session_ref=None, ctx=CTX).status == (
            SubmitStatus.INVALID_PROMPT
        )
        r = b.submit_code(prompt="x", project="nope", session_ref=None, ctx=CTX)
        assert r.status == SubmitStatus.UNKNOWN_PROJECT
        r = b.submit_code(prompt="x", project="other", session_ref="ffffff", ctx=CTX)
        assert r.status == SubmitStatus.UNKNOWN_SESSION
        r = b.submit_code(prompt="do it", project=None, session_ref=None, ctx=CTX)
        assert r.status == SubmitStatus.QUEUED and r.record.discord_channel_id == "3"
        assert r.record.discord_user_id == "1" and r.record.project_id == "default"
    finally:
        await c.stop()


async def test_submit_with_sessions(tmp_path, make_config):
    cfg, c, b = await make(tmp_path, make_config)
    try:
        sessions = SessionRepository(c.conn)
        s = sessions.create(project_id="other", now=NOW)
        r = b.submit_code(prompt="x", project=None, session_ref=s.id[:8], ctx=CTX)
        assert r.status == SubmitStatus.QUEUED
        assert r.record.session_id == s.id and r.record.project_id == "other"
        sessions.close(s.id, now=NOW)
        r = b.submit_code(prompt="x", project=None, session_ref=s.id, ctx=CTX)
        assert r.status == SubmitStatus.SESSION_CLOSED
    finally:
        await c.stop()


async def test_cancel_status_usage_models_sessions(tmp_path, make_config):
    cfg, c, b = await make(tmp_path, make_config)
    try:
        assert b.cancel("abcdef") == (None, "Unknown task.")
        rec = b.submit_code(prompt="x", project=None, session_ref=None, ctx=CTX).record
        await c._task_queue.join()
        result, _ = b.cancel(rec.id[:8])
        assert result == CancelResult.NOT_CANCELLABLE  # already finished by the worker
        snap = await b.status()
        assert snap.opencode.healthy and snap.running is None and len(snap.recent) == 1
        u = b.usage()
        assert u.now and isinstance(u.cooldowns, dict)
        groups = b.models()
        assert [g.policy_id for g in groups] == list(cfg.routing.provider_order)
        assert len(b.sessions()) == 1
    finally:
        await c.stop()
