"""Required 1(c) end to end: a real orchestrator task during the managed serve's crash/backoff
window and after a fatal error fails closed; after a clean relaunch tasks run again.

Real Orchestrator + OpenCodeExecutor + OpenCodeServerManager against the fake serve script, all on
loopback. A foreign server squatting on the fixed port must never receive a request.
"""

from __future__ import annotations

import asyncio
import contextlib
from datetime import UTC, datetime

import pytest
from conftest import secret_env
from test_opencode_server import (
    StubGateway,
    free_port,
    grab_port,
    lifecycle_config,
    make_environ,
    serve_count,
    wait_for,
)

import app.opencode.server as server_mod
from app.bot import format as fmt
from app.db.attempts import FallbackAttemptRepository
from app.db.tasks import TaskRepository
from app.opencode.executor import OpenCodeExecutor
from app.opencode.server import OpenCodeServerManager
from app.orchestrator.orchestrator import Orchestrator
from app.orchestrator.state_machine import TaskStatus
from app.providers.base import ModelCapability
from app.providers.fake import FakeProviderAdapter
from app.runtime.daemon import AgentController, TaskSubmission

PASSWORD = "pw-server-0123456789"


@pytest.fixture(autouse=True)
def _no_real_network() -> None:
    """Override the conftest guard: loopback only."""
    return None


class Recorder:
    def __init__(self):
        self.events = []

    async def start(self, controller):
        return None

    async def stop(self):
        return None

    async def on_event(self, event):
        self.events.append(event)


def build(
    make_config, tmp_path, *, crash: bool, port: int | None = None, scenarios: str = "success"
):
    """Return (cfg, manager, executor, orchestrator, recorder, argv_file)."""
    argv_file = tmp_path / "argv.txt"
    extra = {"FAKE_OC_SERVE_SCENARIO": "crash_after", "FAKE_OC_SERVE_CRASH_AFTER_S": "1.5"}
    base = lifecycle_config(make_config, tmp_path, **({"server_port": port} if port else {}))
    cfg = make_config(
        database={"path": str(tmp_path / "e2e.db")},
        opencode={**base.opencode.model_dump(), "working_directory": str(tmp_path)},
        routing={"max_fallback_attempts": 5},
    )
    manager = OpenCodeServerManager(
        cfg,
        gateway=StubGateway(),  # type: ignore[arg-type]
        environ=make_environ(tmp_path, **(extra if crash else {})),
        password=PASSWORD,
    )
    executor = OpenCodeExecutor(
        cfg.opencode,
        endpoint=lambda: manager.endpoint,
        gateway_base_url=lambda: StubGateway.base_url,
        unavailable_reason=manager.unavailable_reason,
        extra_env={
            "FAKE_OC_SCENARIOS": scenarios,
            "FAKE_OC_ARGV_FILE": str(argv_file),
            "FAKE_OC_COUNTER_FILE": str(tmp_path / "run-counter.txt"),
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
    manager.add_launch_listener(orch.invalidate_server_config_cache)
    return cfg, manager, executor, orch, Recorder(), argv_file


@contextlib.asynccontextmanager
async def running(cfg, orch, rec, manager):
    c = AgentController(cfg, orchestrator=orch, frontend=rec)
    await c.start()
    try:
        yield c
    finally:
        await c.stop()
        await manager.stop()


def submit(c):
    r = c.enqueue_task(
        TaskSubmission(
            project_id="default", prompt="write code", discord_user_id="1", discord_channel_id="42"
        )
    )
    assert r is not None
    return r


async def run_one(c):
    rec = submit(c)
    await asyncio.wait_for(c._task_queue.join(), 30)
    await asyncio.wait_for(c._event_queue.join(), 10)
    return rec


def assert_failed_closed(c, orch, rec, recorder, argv_file, hits):
    t = TaskRepository(c.conn).get(rec.id)
    assert t.status == TaskStatus.FAILED and t.error_class == "OPENCODE_UNAVAILABLE"
    assert FallbackAttemptRepository(c.conn).list_for_task(rec.id) == []
    assert orch.cooldowns.active(datetime.now(UTC)) == {}
    assert not argv_file.exists()  # the executor never spawned `opencode run`
    assert hits == []
    finished = [e for e in recorder.events if e.name == "task_finished" and e.task_id == rec.id]
    assert len(finished) == 1
    text = fmt.format_event(finished[0])
    assert "OpenCode server is not reachable." in text
    assert "managed OpenCode server is down" in text
    assert PASSWORD not in text and "sk-" not in text and "REAL-KEY" not in text
    assert PASSWORD not in str(finished[0].fields)
    return text


async def test_task_in_crash_backoff_window_fails_closed(make_config, tmp_path, monkeypatch):
    monkeypatch.setattr(server_mod, "_INITIAL_BACKOFF_S", 30.0)  # stay in the window
    port = free_port()
    cfg, m, _ex, orch, recorder, argv_file = build(make_config, tmp_path, crash=True, port=port)
    await m.start()
    server = None
    async with running(cfg, orch, recorder, m) as c:
        try:
            orch._server_config_ok_at = datetime.now(UTC)  # a cached server-config OK exists
            await wait_for(lambda: not m.ready)  # crash detected, backoff running
            assert orch._server_config_ok_at is None  # dropped at the crash, not at relaunch
            server, hits = await grab_port(port)
            recs = [await run_one(c), await run_one(c)]
            for rec in recs:
                assert_failed_closed(c, orch, rec, recorder, argv_file, hits)
            assert serve_count(tmp_path) == 1  # still no relaunch
        finally:
            if server is not None:
                server.close()
                await server.wait_closed()


async def test_task_after_fatal_error_fails_closed(make_config, tmp_path, monkeypatch):
    # See test_fatal_error_keeps_health_unhealthy_with_reason: the backoff must outlast grab_port.
    monkeypatch.setattr(server_mod, "_INITIAL_BACKOFF_S", 3.0)
    port = free_port()
    cfg, m, _ex, orch, recorder, argv_file = build(make_config, tmp_path, crash=True, port=port)
    await m.start()
    server = None
    async with running(cfg, orch, recorder, m) as c:
        try:
            await wait_for(lambda: not m.ready)
            server, hits = await grab_port(port)
            assert serve_count(tmp_path) == 1  # grabbed before any relaunch
            await wait_for(lambda: m.fatal_error is not None)
            orch._server_config_ok_at = datetime.now(UTC)  # even a (stale) cached OK
            for _ in range(2):
                rec = await run_one(c)
                text = assert_failed_closed(c, orch, rec, recorder, argv_file, hits)
                assert "already in use" in text
                assert "restart the daemon" in text  # N10
            assert serve_count(tmp_path) == 1
        finally:
            if server is not None:
                server.close()
                await server.wait_closed()


async def test_task_runs_again_after_successful_relaunch(make_config, tmp_path, monkeypatch):
    monkeypatch.setattr(server_mod, "_INITIAL_BACKOFF_S", 0.1)
    monkeypatch.setattr(server_mod, "_BACKOFF_RESET_S", 0.5)
    cfg, m, _ex, orch, recorder, argv_file = build(make_config, tmp_path, crash=True)
    await m.start()
    async with running(cfg, orch, recorder, m) as c:
        await wait_for(lambda: serve_count(tmp_path) == 2 and m.ready)
        rec = await run_one(c)
        t = TaskRepository(c.conn).get(rec.id)
        assert t.status == TaskStatus.SUCCEEDED, t.error_class
        assert argv_file.exists() and len(argv_file.read_text().splitlines()) == 1


async def test_n9_fallback_attempt_after_serve_goes_down_does_not_spawn(
    make_config, tmp_path, monkeypatch
):
    """Attempt 1 fails over (rate limit); the serve then goes fatal; attempt 2 must not spawn."""
    cfg, m, ex, orch, recorder, argv_file = build(
        make_config, tmp_path, crash=False, scenarios="rate_limit,success"
    )
    await m.start()
    real_run = ex.run
    calls: list[int] = []

    async def run_then_go_down(**kw):
        result = await real_run(**kw)
        calls.append(1)
        if len(calls) == 1:
            m.fatal_error = "port 4096 already in use - stop any manually started `opencode serve`"
        return result

    monkeypatch.setattr(ex, "run", run_then_go_down)
    async with running(cfg, orch, recorder, m) as c:
        rec = await run_one(c)
        t = TaskRepository(c.conn).get(rec.id)
        assert t.status == TaskStatus.FAILED and t.error_class == "OPENCODE_UNAVAILABLE"
        attempts = FallbackAttemptRepository(c.conn).list_for_task(rec.id)
        assert len(attempts) == 2
        assert attempts[1].status.value == "EXECUTOR_UNAVAILABLE"
        assert len(argv_file.read_text().splitlines()) == 1  # only attempt 1 ever spawned
        assert calls == [1, 1]
        cooling = orch.cooldowns.active(datetime.now(UTC))
        assert {p for p, _ in cooling} == {attempts[0].provider}  # attempt 1 only
        finished = [e for e in recorder.events if e.name == "task_finished" and e.task_id == rec.id]
        assert len(finished) == 1
        text = fmt.format_event(finished[0])
        assert "managed OpenCode server is down" in text and "restart the daemon" in text
        assert PASSWORD not in text
