from __future__ import annotations

import logging

from conftest import NOW
from test_bot_format import task

from app.bot.commands import (
    CommandContext,
    StatusSnapshot,
    SubmitResult,
    SubmitStatus,
    UsageSnapshot,
    run_command,
)
from app.config.models import DiscordConfig
from app.db.sessions import SessionRecord, SessionStatus
from app.orchestrator.orchestrator import CancelResult
from app.redaction import SecretRedactor
from app.runtime.interfaces import ExecutorHealth

CFG = DiscordConfig(allowed_user_ids=[1], allowed_guild_ids=[2], allowed_channel_ids=[3])
OK = CommandContext(1, 2, 3)
SECRET = "sk-test-SECRET-0123456789"


class FakeBackend:
    def __init__(self):
        self.calls = []
        self.submit_result = SubmitResult(SubmitStatus.QUEUED, task(), 1)
        self.cancel_result = (CancelResult.CANCELLING, "")
        self.boom = None
        self.session_text = "default"

    def _check(self):
        if self.boom:
            raise self.boom

    def submit_code(self, *, prompt, project, session_ref, ctx):
        self.calls.append(("code", prompt, project, session_ref))
        self._check()
        return self.submit_result

    def cancel(self, task_ref):
        self.calls.append(("cancel", task_ref))
        self._check()
        return self.cancel_result

    async def status(self):
        self.calls.append(("status",))
        self._check()
        return StatusSnapshot(ExecutorHealth(True, "1", "ok"), None, 0, ())

    def usage(self):
        self.calls.append(("usage",))
        self._check()
        return UsageSnapshot((), {}, (), NOW)

    def models(self):
        self.calls.append(("models",))
        self._check()
        return []

    def sessions(self, limit=10):
        self.calls.append(("sessions",))
        self._check()
        return [
            SessionRecord("abcdef12", None, self.session_text, SessionStatus.OPEN, NOW, NOW, None)
        ]


async def cmd(name, backend, ctx=OK, redactor=None, **options):
    return await run_command(
        name, cfg=CFG, backend=backend, ctx=ctx, redactor=redactor, options=options
    )


async def test_unauthorized_no_backend_call_and_prompt_not_logged(caplog):
    b = FakeBackend()
    with caplog.at_level(logging.DEBUG):
        r = await cmd("code", b, CommandContext(99, 2, 3), prompt="TOP-SECRET-PROMPT-TEXT")
    assert r.content == "Not authorized." and r.ephemeral
    assert b.calls == []
    assert any("auth denied" in rec.getMessage() for rec in caplog.records)
    assert all("TOP-SECRET-PROMPT-TEXT" not in rec.getMessage() for rec in caplog.records)


async def test_unknown_command():
    r = await cmd("bogus", FakeBackend())
    assert r.content == "Unknown command." and r.ephemeral


async def test_code_queued_non_ephemeral():
    b = FakeBackend()
    r = await cmd("code", b, prompt="hello", project=None, session=None)
    assert "abcdef12" in r.content and not r.ephemeral
    assert b.calls == [("code", "hello", "default", None)]


async def test_code_session_ref_lowercased_and_validated():
    b = FakeBackend()
    await cmd("code", b, prompt="x", session="ABCDEF12")
    assert b.calls[0][3] == "abcdef12"
    b2 = FakeBackend()
    r = await cmd("code", b2, prompt="x", session="a'; DROP TABLE--")
    assert b2.calls == [] and "Unknown session" in r.content


async def test_each_submit_status_has_reply():
    seen = set()
    for status in SubmitStatus:
        b = FakeBackend()
        b.submit_result = SubmitResult(status, task() if status == SubmitStatus.QUEUED else None)
        r = await cmd("code", b, prompt="x")
        assert r.content and not r.ephemeral
        seen.add(r.content)
    assert len(seen) == len(SubmitStatus)


async def test_cancel():
    b = FakeBackend()
    r = await cmd("cancel", b, task="not valid!")
    assert r.content == "Invalid task id." and b.calls == []
    r = await cmd("cancel", b, task="ABCDEF12")
    assert b.calls == [("cancel", "abcdef12")] and r.ephemeral
    b.cancel_result = (None, "ambiguous id")
    r = await cmd("cancel", b, task="abcdef12")
    assert r.content == "ambiguous id"


async def test_read_commands_are_ephemeral():
    for name in ("status", "usage", "models", "sessions"):
        r = await cmd(name, FakeBackend())
        assert r.ephemeral and r.content


async def test_backend_exception_becomes_internal_error(caplog):
    b = FakeBackend()
    b.boom = RuntimeError("secret detail")
    with caplog.at_level(logging.DEBUG):
        r = await cmd("status", b)
    assert r.content == "Internal error (RuntimeError)."
    assert all("secret detail" not in rec.getMessage() for rec in caplog.records)


async def test_secret_in_backend_data_redacted():
    b = FakeBackend()
    b.session_text = f"proj-{SECRET}"
    r = await cmd("sessions", b, redactor=SecretRedactor([SECRET]))
    assert SECRET not in r.content and "[REDACTED]" in r.content
