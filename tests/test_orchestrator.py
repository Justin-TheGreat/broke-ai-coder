from __future__ import annotations

import asyncio
import contextlib
import sys
from pathlib import Path

import httpx
from conftest import NOW, secret_env
from fakes.fake_executor import HEALTHY, ScriptedExecutor, ok

from app.db.attempts import AttemptStatus, FallbackAttemptRepository
from app.db.connection import open_database
from app.db.migrations import migrate
from app.db.sessions import SessionRepository
from app.db.tasks import TaskRepository
from app.opencode.config_check import allowed_small_models
from app.opencode.executor import OpenCodeExecutor
from app.orchestrator.orchestrator import CancelResult, Orchestrator
from app.orchestrator.state_machine import TaskStatus
from app.providers.base import ErrorClass, ModelCapability
from app.providers.fake import FakeProviderAdapter
from app.runtime.daemon import AgentController, TaskSubmission
from app.runtime.interfaces import (
    ExecutionOutcome,
    ExecutionResult,
    ExecutorEvent,
    ExecutorEventKind,
    ExecutorHealth,
    SmallModelCheck,
)

FAKE = str(Path(__file__).parent / "fakes" / "fake_opencode.py")


def failed(ec=ErrorClass.RATE_LIMITED, *, session=None, status=429, retry=None, tools=0):
    return ExecutionResult(
        outcome=ExecutionOutcome.FAILED,
        exit_code=1,
        error_class=ec,
        session_id=session,
        http_status=status,
        retry_after_s=retry,
        tool_events=tools,
    )


def build(
    tmp_path,
    make_config,
    results,
    *,
    health=HEALTHY,
    routing=None,
    orchestrator=None,
    small_model=SmallModelCheck.OK,
):
    overrides = {
        "database": {"path": str(tmp_path / "o.db")},
        "opencode": {"working_directory": str(tmp_path)},
        "routing": {"max_fallback_attempts": 5, **(routing or {})},
    }
    if orchestrator:
        overrides["orchestrator"] = orchestrator
    cfg = make_config(**overrides)
    adapters = {}
    for pol in cfg.routing.policies.values():
        adapters[pol.provider] = FakeProviderAdapter(
            pol.provider,
            models=[
                ModelCapability(
                    pol.provider,
                    m,
                    supports_tool_calling=True,
                    context_window=200_000,
                    max_output_tokens=32_000,
                )
                for m in pol.model_order
            ],
        )
    executor = ScriptedExecutor(results, health, small_model)
    orch = Orchestrator(
        cfg, executor=executor, adapters=adapters, environ=secret_env(), clock=lambda: NOW
    )
    return cfg, executor, orch


class Recorder:
    def __init__(self):
        self.events = []

    async def start(self, controller):
        return None

    async def stop(self):
        return None

    async def on_event(self, event):
        self.events.append(event)


@contextlib.asynccontextmanager
async def started(cfg, orch, frontend=None):
    c = AgentController(cfg, orchestrator=orch, frontend=frontend)
    await c.start()
    try:
        yield c
    finally:
        await c.stop()


def submit(c, prompt="write code", project="default", session_id=None):
    rec = c.enqueue_task(
        TaskSubmission(
            project_id=project,
            prompt=prompt,
            discord_user_id="1",
            discord_channel_id="42",
            session_id=session_id,
        )
    )
    assert rec is not None
    return rec


async def drain(c):
    await asyncio.wait_for(c._task_queue.join(), 10)


def get(c, task_id):
    return TaskRepository(c.conn).get(task_id)


async def test_success(tmp_path, make_config):
    cfg, ex, orch = build(tmp_path, make_config, [ok(session_id="ses_a", llm_steps=1)])
    async with started(cfg, orch) as c:
        rec = submit(c)
        await drain(c)
        t = get(c, rec.id)
        assert t.status == TaskStatus.SUCCEEDED
        assert t.cost_class.value == "FREE"
        assert (t.selected_provider, t.selected_model) == ("openrouter", "openrouter/free")
        atts = FallbackAttemptRepository(c.conn).list_for_task(rec.id)
        assert [a.status for a in atts] == [AttemptStatus.SUCCEEDED]
        rows = c.conn.execute("SELECT cost_class, status FROM usage_events").fetchall()
        assert [(r[0], r[1]) for r in rows] == [("FREE", "success")]
        assert orch.running is None
        sess = SessionRepository(c.conn).get(t.session_id)
        assert sess.opencode_session_id == "ses_a" and sess.status.value == "OPEN"


async def test_fallback_chain(tmp_path, make_config):
    results = [
        failed(session="ses_fake1", retry=30),
        failed(session="ses_fake1"),
        ok(session_id="ses_fake1"),
    ]
    cfg, ex, orch = build(tmp_path, make_config, results)
    rec_events = Recorder()
    async with started(cfg, orch, rec_events) as c:
        rec = submit(c, prompt="original prompt")
        await drain(c)
        t = get(c, rec.id)
        assert t.status == TaskStatus.SUCCEEDED
        atts = FallbackAttemptRepository(c.conn).list_for_task(rec.id)
        assert [(a.provider, a.model) for a in atts] == [
            ("openrouter", "openrouter/free"),
            ("gemini", "g-3.8"),
            ("gemini", "g-3.7"),
        ]
        assert [a.error_class for a in atts] == ["RATE_LIMITED", "RATE_LIMITED", None]
        assert [a.http_status for a in atts] == [429, 429, None]
        assert atts[0].retry_after_ms == 30000
        cool = orch.cooldowns.active(NOW)
        assert ("openrouter", "openrouter/free") in cool and ("gemini", "g-3.8") in cool
        assert ex.calls[0]["prompt"] == "original prompt" and ex.calls[0]["session_id"] is None
        for call in ex.calls[1:]:
            assert call["session_id"] == "ses_fake1"
            assert call["prompt"] == cfg.opencode.continue_prompt
        sess = SessionRepository(c.conn).get(t.session_id)
        assert sess.opencode_session_id == "ses_fake1"
        paid = c.conn.execute("SELECT COUNT(*) FROM usage_events WHERE cost_class <> 'FREE'")
        assert paid.fetchone()[0] == 0
        await c._event_queue.join()
        names = [e.name for e in rec_events.events]
        assert names.count("attempt_failed") == 2 and names.count("provider_selected") == 3
        assert names[-1] == "task_finished"


async def test_no_session_resends_original_prompt(tmp_path, make_config):
    cfg, ex, orch = build(tmp_path, make_config, [failed(), ok()])
    async with started(cfg, orch) as c:
        rec = submit(c, prompt="orig")
        await drain(c)
        assert get(c, rec.id).status == TaskStatus.SUCCEEDED
        assert [x["prompt"] for x in ex.calls] == ["orig", "orig"]
        assert [x["session_id"] for x in ex.calls] == [None, None]


async def test_tool_activity_without_session_stops_unknown(tmp_path, make_config):
    cfg, ex, orch = build(tmp_path, make_config, [failed(tools=2), ok()])
    async with started(cfg, orch) as c:
        rec = submit(c)
        await drain(c)
        t = get(c, rec.id)
        assert t.status == TaskStatus.FAILED and t.error_class == "UNKNOWN"
        assert len(ex.calls) == 1


async def test_every_candidate_rate_limited(tmp_path, make_config):
    cfg, ex, orch = build(tmp_path, make_config, [failed()], routing={"max_fallback_attempts": 10})
    async with started(cfg, orch) as c:
        rec = submit(c)
        await drain(c)
        t = get(c, rec.id)
        assert t.status == TaskStatus.FAILED and t.error_class == "FREE_CAPACITY_EXHAUSTED"
        assert len(ex.calls) == 5
        assert len(FallbackAttemptRepository(c.conn).list_for_task(rec.id)) == 5


async def test_max_fallback_attempts_reached(tmp_path, make_config):
    cfg, ex, orch = build(tmp_path, make_config, [failed()], routing={"max_fallback_attempts": 2})
    async with started(cfg, orch) as c:
        rec = submit(c)
        await drain(c)
        t = get(c, rec.id)
        assert t.status == TaskStatus.FAILED and t.error_class == "MAX_FALLBACK_ATTEMPTS_REACHED"
        assert len(ex.calls) == 2


async def test_timed_out_is_not_replayed(tmp_path, make_config):
    res = ExecutionResult(
        outcome=ExecutionOutcome.TIMED_OUT,
        error_class=ErrorClass.TIMEOUT_UNKNOWN_OUTCOME,
        session_id="ses_t",
        llm_steps=2,
    )
    cfg, ex, orch = build(tmp_path, make_config, [res, ok()])
    async with started(cfg, orch) as c:
        rec = submit(c)
        await drain(c)
        t = get(c, rec.id)
        assert t.status == TaskStatus.FAILED and t.error_class == "TIMEOUT_UNKNOWN_OUTCOME"
        assert len(ex.calls) == 1
        assert len(FallbackAttemptRepository(c.conn).list_for_task(rec.id)) == 1
        statuses = [r[0] for r in c.conn.execute("SELECT status FROM usage_events ORDER BY id")]
        assert statuses == ["success", "unknown_outcome"]


async def test_auth_failure_moves_to_next_provider(tmp_path, make_config):
    cfg, ex, orch = build(tmp_path, make_config, [failed(ErrorClass.AUTH_FAILED, status=401), ok()])
    async with started(cfg, orch) as c:
        rec = submit(c)
        await drain(c)
        assert get(c, rec.id).status == TaskStatus.SUCCEEDED
        assert ex.calls[1]["provider"] == "gemini"


async def test_unhealthy_executor(tmp_path, make_config):
    cfg, ex, orch = build(
        tmp_path, make_config, [ok()], health=ExecutorHealth(False, None, "HTTP 500")
    )
    async with started(cfg, orch) as c:
        rec = submit(c)
        await drain(c)
        t = get(c, rec.id)
        assert t.status == TaskStatus.FAILED and t.error_class == "OPENCODE_UNAVAILABLE"
        assert ex.calls == []
        assert orch.cooldowns.active(NOW) == {}
        assert FallbackAttemptRepository(c.conn).list_for_task(rec.id) == []


async def test_executor_unavailable_result(tmp_path, make_config):
    res = ExecutionResult(outcome=ExecutionOutcome.EXECUTOR_UNAVAILABLE, detail="missing")
    cfg, ex, orch = build(tmp_path, make_config, [res, ok()])
    async with started(cfg, orch) as c:
        rec = submit(c)
        await drain(c)
        t = get(c, rec.id)
        assert t.status == TaskStatus.FAILED and t.error_class == "OPENCODE_UNAVAILABLE"
        assert len(ex.calls) == 1 and orch.cooldowns.active(NOW) == {}
        atts = FallbackAttemptRepository(c.conn).list_for_task(rec.id)
        assert atts[0].status == AttemptStatus.EXECUTOR_UNAVAILABLE
        assert c.conn.execute("SELECT COUNT(*) FROM usage_events").fetchone()[0] == 0


async def _wait_for(pred, timeout=5.0):
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while not pred():
        assert loop.time() < end, "timed out waiting"
        await asyncio.sleep(0.01)


async def test_cancel_while_running(tmp_path, make_config):
    async def hang(**kw):
        await kw["cancel_event"].wait()
        return ExecutionResult(outcome=ExecutionOutcome.CANCELLED)

    cfg, ex, orch = build(tmp_path, make_config, [hang])
    async with started(cfg, orch) as c:
        rec = submit(c)
        await _wait_for(lambda: len(ex.calls) == 1)
        assert orch.running is not None and orch.running.task_id == rec.id
        assert c.metrics()["running_task_id"] == rec.id
        assert c.cancel_task(rec.id) == CancelResult.CANCELLING
        await drain(c)
        t = get(c, rec.id)
        assert t.status == TaskStatus.CANCELLED and t.error_class is None
        assert c.cancel_task(rec.id) == CancelResult.NOT_CANCELLABLE
        assert c.cancel_task("nope") == CancelResult.NOT_FOUND


async def test_cancel_while_queued(tmp_path, make_config):
    gate = asyncio.Event()

    async def blocked(**kw):
        await gate.wait()
        return ok()

    cfg, ex, orch = build(tmp_path, make_config, [blocked])
    async with started(cfg, orch) as c:
        first = submit(c, "one")
        await _wait_for(lambda: len(ex.calls) == 1)
        second = submit(c, "two")
        assert c.cancel_task(second.id) == CancelResult.CANCELLED
        gate.set()
        await drain(c)
        assert get(c, first.id).status == TaskStatus.SUCCEEDED
        assert get(c, second.id).status == TaskStatus.CANCELLED
        assert len(ex.calls) == 1


def test_recover_interrupted(tmp_path, make_config):
    cfg, ex, orch = build(tmp_path, make_config, [ok()])
    conn = open_database(cfg.database.path)
    migrate(conn)
    tasks = TaskRepository(conn)
    running = tasks.create(project_id="default", prompt="a", discord_user_id="u", now=NOW)
    tasks.transition(running.id, TaskStatus.ROUTING, now=NOW)
    tasks.transition(running.id, TaskStatus.RUNNING, now=NOW)
    queued = tasks.create(project_id="default", prompt="b", discord_user_id="u", now=NOW)
    conn.close()

    async def go():
        async with started(cfg, orch) as c:
            assert get(c, running.id).status == TaskStatus.FAILED
            assert get(c, running.id).error_class == "INTERRUPTED_BY_RESTART"
            assert get(c, queued.id).status == TaskStatus.CANCELLED
            assert get(c, queued.id).error_class == "INTERRUPTED_BY_RESTART"
            assert ex.calls == []

    asyncio.run(go())


async def test_shutdown_during_run(tmp_path, make_config):
    async def hang(**kw):
        await kw["cancel_event"].wait()
        return ExecutionResult(outcome=ExecutionOutcome.CANCELLED)

    cfg, ex, orch = build(tmp_path, make_config, [hang])
    c = AgentController(cfg, orchestrator=orch)
    await c.start()
    rec = submit(c)
    queued = submit(c, "later")
    await _wait_for(lambda: len(ex.calls) == 1)
    conn_path = cfg.database.path
    await c.stop()
    conn = open_database(conn_path)
    try:
        repo = TaskRepository(conn)
        t = repo.get(rec.id)
        assert t.status == TaskStatus.CANCELLED and t.error_class == "DAEMON_SHUTDOWN"
        q = repo.get(queued.id)
        assert q.status == TaskStatus.CANCELLED and q.error_class == "DAEMON_SHUTDOWN"
    finally:
        conn.close()
    assert len(ex.calls) == 1


async def test_follow_up_uses_existing_opencode_session(tmp_path, make_config):
    cfg, ex, orch = build(tmp_path, make_config, [ok()])
    async with started(cfg, orch) as c:
        sessions = SessionRepository(c.conn)
        s = sessions.create(project_id="default", now=NOW)
        sessions.set_opencode_session_id(s.id, "ses_prev", now=NOW)
        rec = submit(c, "next step", session_id=s.id)
        await drain(c)
        assert get(c, rec.id).status == TaskStatus.SUCCEEDED
        assert ex.calls[0]["session_id"] == "ses_prev" and ex.calls[0]["prompt"] == "next step"
        assert get(c, rec.id).session_id == s.id


async def test_closed_session_rejected(tmp_path, make_config):
    cfg, ex, orch = build(tmp_path, make_config, [ok()])
    async with started(cfg, orch) as c:
        sessions = SessionRepository(c.conn)
        s = sessions.create(project_id="default", now=NOW)
        sessions.close(s.id, now=NOW)
        rec = submit(c, session_id=s.id)
        await drain(c)
        t = get(c, rec.id)
        assert t.status == TaskStatus.FAILED and t.error_class == "INVALID_REQUEST"


async def test_session_without_opencode_id_closed_on_failure(tmp_path, make_config):
    cfg, ex, orch = build(
        tmp_path, make_config, [ExecutionResult(ExecutionOutcome.EXECUTOR_UNAVAILABLE)]
    )
    async with started(cfg, orch) as c:
        rec = submit(c)
        await drain(c)
        sess = SessionRepository(c.conn).get(get(c, rec.id).session_id)
        assert sess.status.value == "CLOSED"


async def test_unknown_project(tmp_path, make_config):
    cfg, ex, orch = build(tmp_path, make_config, [ok()])
    async with started(cfg, orch) as c:
        rec = submit(c, project="nope")
        await drain(c)
        t = get(c, rec.id)
        assert t.status == TaskStatus.FAILED and t.error_class == "INVALID_REQUEST"
        assert ex.calls == []
        assert FallbackAttemptRepository(c.conn).list_for_task(rec.id) == []


async def test_progress_events_throttled(tmp_path, make_config):
    async def noisy(**kw):
        cb = kw["on_event"]
        cb(ExecutorEvent(ExecutorEventKind.SESSION, session_id="ses_p"))
        for _ in range(50):
            cb(ExecutorEvent(ExecutorEventKind.TOOL, tool="bash", tool_status="running"))
        return ok(session_id="ses_p")

    cfg, ex, orch = build(
        tmp_path, make_config, [noisy], orchestrator={"progress_min_interval_s": 10}
    )
    rec_events = Recorder()
    async with started(cfg, orch, rec_events) as c:
        submit(c)
        await drain(c)
        await c._event_queue.join()
    progress = [e for e in rec_events.events if e.name == "progress"]
    assert len(progress) == 2
    assert progress[0].fields["opencode_session_id"] == "ses_p"
    assert progress[1].fields["tool"] == "bash"
    for e in rec_events.events:
        if e.task_id and e.name != "task_queued":
            assert e.fields["channel_id"] == "42" and len(e.fields["short_id"]) == 8


async def test_real_executor_integration(tmp_path, make_config):
    cfg = make_config(
        database={"path": str(tmp_path / "o.db")},
        opencode={"working_directory": str(tmp_path), "command": [sys.executable, FAKE]},
        routing={"max_fallback_attempts": 5},
    )
    safe = sorted(allowed_small_models(cfg))[0]

    def handler(req):
        if req.url.path == "/config":
            return httpx.Response(200, json={"small_model": safe})
        return httpx.Response(200, json={"healthy": True})

    transport = httpx.MockTransport(handler)
    executor = OpenCodeExecutor(
        cfg.opencode,
        transport=transport,
        extra_env={
            "FAKE_OC_SCENARIOS": "rate_limit,success",
            "FAKE_OC_ARGV_FILE": str(tmp_path / "argv.txt"),
            "FAKE_OC_COUNTER_FILE": str(tmp_path / "counter.txt"),
        },
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
    orch = Orchestrator(cfg, executor=executor, adapters=adapters, environ=secret_env())
    async with started(cfg, orch) as c:
        rec = submit(c)
        await asyncio.wait_for(c._task_queue.join(), 60)
        t = get(c, rec.id)
        assert t.status == TaskStatus.SUCCEEDED
        assert (t.selected_provider, t.selected_model) == ("gemini", "g-3.8")
    import json

    lines = [json.loads(x) for x in (tmp_path / "argv.txt").read_text().splitlines()]
    assert len(lines) == 2
    assert "--session" not in lines[0]
    assert lines[0][lines[0].index("--model") + 1] == "openrouter/openrouter/free"
    assert lines[1][lines[1].index("--session") + 1] == "ses_fake1"
    assert lines[1][lines[1].index("--model") + 1] == "google/g-3.8"


async def test_unsafe_small_model_fails_closed(tmp_path, make_config):
    cfg, ex, orch = build(tmp_path, make_config, [ok()], small_model=SmallModelCheck.UNSAFE)
    async with started(cfg, orch) as c:
        rec = submit(c)
        await drain(c)
        t = get(c, rec.id)
        assert t.status == TaskStatus.FAILED and t.error_class == "OPENCODE_UNSAFE_CONFIG"
        assert ex.calls == []
        assert orch.cooldowns.active(NOW) == {}
        assert FallbackAttemptRepository(c.conn).list_for_task(rec.id) == []


async def test_small_model_config_unavailable_is_opencode_unavailable(tmp_path, make_config):
    cfg, ex, orch = build(tmp_path, make_config, [ok()], small_model=SmallModelCheck.UNAVAILABLE)
    async with started(cfg, orch) as c:
        rec = submit(c)
        await drain(c)
        t = get(c, rec.id)
        assert t.status == TaskStatus.FAILED and t.error_class == "OPENCODE_UNAVAILABLE"
        assert ex.calls == []


async def test_small_model_ok_is_cached_for_ttl(tmp_path, make_config):
    cfg, ex, orch = build(tmp_path, make_config, [ok()])
    async with started(cfg, orch) as c:
        submit(c)
        submit(c)
        await drain(c)
        assert len(ex.small_model_calls) == 1 and len(ex.calls) == 2
        assert "openrouter/openrouter/free" in ex.small_model_calls[0]
