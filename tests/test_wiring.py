from __future__ import annotations

from app.opencode.executor import OpenCodeExecutor
from app.orchestrator.orchestrator import Orchestrator
from app.redaction import SecretRedactor
from app.runtime.interfaces import NullFrontend
from app.runtime.wiring import build_runtime, close_runtime


def cfg(make_config, tmp_path, **opencode):
    return make_config(
        database={"path": str(tmp_path / "w.db")},
        opencode={"managed_server": False, **opencode},
    )


async def test_build_runtime_without_token(make_config, tmp_path):
    rt = build_runtime(cfg(make_config, tmp_path), redactor=SecretRedactor(), environ={})
    assert isinstance(rt.controller._frontend, NullFrontend)
    assert isinstance(rt.executor, OpenCodeExecutor)
    assert isinstance(rt.orchestrator, Orchestrator)
    assert rt.controller.orchestrator is rt.orchestrator
    assert set(rt.adapters) == {"openrouter", "gemini", "groq"}
    await rt.controller.start()
    try:
        assert rt.orchestrator._controller is rt.controller
    finally:
        await rt.controller.stop()
    closed = []
    for name, adapter in rt.adapters.items():
        orig = adapter.aclose

        async def spy(name=name, orig=orig):
            closed.append(name)
            await orig()

        adapter.aclose = spy
    await close_runtime(rt)
    assert sorted(closed) == ["gemini", "groq", "openrouter"]


async def test_once_always_null_frontend(make_config, tmp_path):
    rt = build_runtime(
        cfg(make_config, tmp_path),
        redactor=SecretRedactor(),
        environ={"DISCORD_BOT_TOKEN": "fake-token-value-123456"},
        once=True,
    )
    assert isinstance(rt.controller._frontend, NullFrontend)
    await close_runtime(rt)


async def test_token_gives_discord_frontend(make_config, tmp_path):
    from app.bot.discord_frontend import DiscordFrontend

    rt = build_runtime(
        cfg(make_config, tmp_path),
        redactor=SecretRedactor(),
        environ={"DISCORD_BOT_TOKEN": "fake-token-value-123456"},
    )
    assert isinstance(rt.controller._frontend, DiscordFrontend)
    await close_runtime(rt)


async def test_services_order_and_shared_cooldowns(make_config, tmp_path):
    rt = build_runtime(
        cfg(make_config, tmp_path, managed_server=True), redactor=SecretRedactor(), environ={}
    )
    try:
        assert rt.server_manager is not None
        assert rt.controller._services == [rt.gateway, rt.launcher, rt.server_manager]
        # one CooldownManager shared by the gateway, the orchestrator and the router state
        assert rt.gateway._cooldowns is rt.orchestrator.cooldowns
    finally:
        await close_runtime(rt)


async def test_managed_server_false_has_no_manager(make_config, tmp_path):
    rt = build_runtime(cfg(make_config, tmp_path), redactor=SecretRedactor(), environ={})
    try:
        assert rt.server_manager is None
        assert rt.controller._services == [rt.gateway, rt.launcher]
        ep = rt.executor._endpoint()
        assert ep.url == "http://127.0.0.1:4096" and ep.password is None
    finally:
        await close_runtime(rt)


async def test_redactor_holds_gateway_token_and_server_password(make_config, tmp_path):
    redactor = SecretRedactor()
    rt = build_runtime(
        cfg(make_config, tmp_path, managed_server=True), redactor=redactor, environ={}
    )
    try:
        assert rt.server_manager is not None
        text = f"t={rt.gateway.token} p={rt.server_manager.password}"
        out = redactor.redact(text)
        assert rt.gateway.token not in out and rt.server_manager.password not in out
        assert rt.gateway._redactor is redactor
    finally:
        await close_runtime(rt)


async def test_executor_child_env_has_no_bot_token_or_keys(make_config, tmp_path):
    environ = {
        "PATH": "/bin",
        "DISCORD_BOT_TOKEN": "fake-token-value-123456",
        "GROQ_API_KEY": "fake-groq-key-123456",
    }
    rt = build_runtime(cfg(make_config, tmp_path), redactor=SecretRedactor(), environ=environ)
    try:
        env = rt.executor._child_env()
        assert "DISCORD_BOT_TOKEN" not in env and "GROQ_API_KEY" not in env
        assert env["PATH"] == "/bin"
    finally:
        await close_runtime(rt)


async def test_gateway_usage_sink_writes_rows_with_running_task(make_config, tmp_path):
    from datetime import UTC, datetime

    from app.db.usage import UsageEvent, UsageStatus
    from app.orchestrator.orchestrator import RunningTask
    from app.providers.base import CostClass

    rt = build_runtime(cfg(make_config, tmp_path), redactor=SecretRedactor(), environ={})
    await rt.controller.start()
    try:
        ev = UsageEvent("groq", "q-x", CostClass.FREE, UsageStatus.SUCCESS, datetime.now(UTC))
        rt.gateway._usage_sink(ev)
        assert rt.gateway._current_task_id() is None
        rt.orchestrator._running = RunningTask("task-xyz", "", None, None, 0, None, ev.occurred_at)
        assert rt.gateway._current_task_id() == "task-xyz"
        rows = rt.controller.conn.execute("SELECT provider, status FROM usage_events").fetchall()
        assert [tuple(r) for r in rows] == [("groq", "success")]
        # services started: the gateway is listening
        assert rt.gateway.base_url.startswith("http://127.0.0.1:")
    finally:
        await rt.controller.stop()
        await close_runtime(rt)


async def test_once_has_no_manager_and_no_services(make_config, tmp_path):
    rt = build_runtime(
        cfg(make_config, tmp_path, managed_server=True),
        redactor=SecretRedactor(),
        environ={},
        once=True,
    )
    try:
        assert rt.server_manager is None
        assert rt.controller._services == []
    finally:
        await close_runtime(rt)


async def test_manager_launch_invalidates_orchestrator_server_config_cache(make_config, tmp_path):
    from datetime import UTC, datetime

    rt = build_runtime(
        cfg(make_config, tmp_path, managed_server=True), redactor=SecretRedactor(), environ={}
    )
    try:
        assert rt.server_manager is not None
        rt.orchestrator._server_config_ok_at = datetime.now(UTC)
        rt.server_manager._notify_launch()
        assert rt.orchestrator._server_config_ok_at is None
    finally:
        await close_runtime(rt)


async def test_managed_executor_unhealthy_until_manager_ready(make_config, tmp_path):
    rt = build_runtime(
        cfg(make_config, tmp_path, managed_server=True), redactor=SecretRedactor(), environ={}
    )
    try:
        health = await rt.executor.health_check()  # manager never started: no attach, no request
        assert not health.healthy
        assert "managed OpenCode server is down" in health.detail
    finally:
        await close_runtime(rt)
