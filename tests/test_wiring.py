from __future__ import annotations

from app.opencode.executor import OpenCodeExecutor
from app.orchestrator.orchestrator import Orchestrator
from app.redaction import SecretRedactor
from app.runtime.interfaces import NullFrontend
from app.runtime.wiring import build_runtime, close_runtime


def cfg(make_config, tmp_path):
    return make_config(database={"path": str(tmp_path / "w.db")})


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
