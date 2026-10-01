from __future__ import annotations

from datetime import timedelta

from conftest import NOW

from app.bot import format as fmt
from app.bot.commands import ProviderModels, StatusSnapshot, UsageSnapshot
from app.db.sessions import SessionRecord, SessionStatus
from app.db.tasks import TaskRecord
from app.db.usage import UsageTotals
from app.orchestrator.orchestrator import CancelResult, RunningTask
from app.orchestrator.state_machine import TaskStatus
from app.providers.base import ModelCapability, QuotaConfidence, QuotaRecord, QuotaUnit
from app.redaction import SecretRedactor
from app.router.policy import ModelListing, ModelRoutingStatus
from app.runtime.daemon import ControllerEvent
from app.runtime.interfaces import ExecutorHealth

SECRET = "sk-test-SECRET-0123456789"


def ev(name, **fields):
    return ControllerEvent(name, at=NOW, task_id="abcdef1234", fields=fields)


def task(**kw):
    base = dict(
        id="abcdef12-0000", project_id="default", session_id=None, discord_guild_id=None,
        discord_channel_id=None, discord_user_id="u", prompt="p", status=TaskStatus.QUEUED,
        selected_provider=None, selected_model=None, cost_class=None, created_at=NOW,
        started_at=None, finished_at=None, last_event_at=NOW, exit_code=None, error_class=None,
    )  # fmt: skip
    base.update(kw)
    return TaskRecord(**base)


def test_safe_text_truncates_neutralizes_strips_redacts():
    r = SecretRedactor([SECRET])
    out = fmt.safe_text("x" * 5000, None)
    assert len(out) <= 1900 and out.endswith("(truncated)")
    assert "```" not in fmt.safe_text("a ``` b", None)
    assert fmt.safe_text("\x1b[31mred\x1b[0m", None) == "red"
    assert SECRET not in fmt.safe_text(f"key {SECRET}", r)


def test_ack():
    out = fmt.format_ack(task(), 2)
    assert out == "Task abcdef12 queued (position 2). Project: default."


def test_routing_and_queued_are_none():
    assert fmt.format_event(ev("task_routing")) is None
    assert fmt.format_event(ev("task_queued")) is None
    assert fmt.format_event(ev("unknown_thing")) is None


def test_provider_selected_and_attempt_failed():
    out = fmt.format_event(ev("provider_selected", provider="gemini", model="g", attempt_no=2))
    assert out == "Provider: gemini / g (attempt 2)"
    out = fmt.format_event(ev("attempt_failed", attempt_no=1, error_class="RATE_LIMITED"))
    assert out.startswith("Attempt 1 failed: RATE_LIMITED")


def test_progress():
    assert fmt.format_event(ev("progress", opencode_session_id="s")) == "OpenCode session started"
    out = fmt.format_event(ev("progress", tool="bash", tool_status="running", llm_steps=3))
    assert out == "Working… bash (running), steps 3"


def test_finished_success_with_code_block_and_redaction():
    r = SecretRedactor([SECRET])
    out = fmt.format_event(
        ev(
            "task_finished", status="SUCCEEDED", short_id="abcdef12", provider="p", model="m",
            changed_files="a.py,b.py", output_tail=f"result {SECRET}\n```inner```",
        ),
        redactor=r,
    )  # fmt: skip
    assert "✅ Task abcdef12 completed" in out
    assert "Changed: a.py, b.py" in out and "Cost class: FREE" in out
    assert SECRET not in out
    assert out.count("```") == 2  # only our fence; inner one neutralized


def test_finished_failures():
    capacity = fmt.format_event(
        ev("task_finished", status="FAILED", error_class="FREE_CAPACITY_EXHAUSTED")
    )
    assert "No free capacity left on any configured provider. Nothing was paid for." in capacity
    unknown = fmt.format_event(
        ev("task_finished", status="FAILED", error_class="UNKNOWN", session_short="abcd1234")
    )
    assert "Outcome unknown; not replayed. Continue with /code session:abcd1234" in unknown
    unavailable = fmt.format_event(
        ev("task_finished", status="FAILED", error_class="OPENCODE_UNAVAILABLE")
    )
    assert "OpenCode server is not reachable." in unavailable
    cancelled = fmt.format_event(ev("task_finished", status="CANCELLED", short_id="abcdef12"))
    assert cancelled == "🛑 Task abcdef12 cancelled"


def q(conf, remaining, model=None, window="day"):
    return QuotaRecord(
        "gemini", model, window, QuotaUnit.REQUESTS, conf, NOW, "s", limit=100, remaining=remaining
    )


def test_usage_variants():
    snap = UsageSnapshot(
        quota=(
            q(QuotaConfidence.EXACT, 42),
            q(QuotaConfidence.ESTIMATED, 7, model="g1", window="minute"),
            q(QuotaConfidence.UNKNOWN, None, window="hour"),
        ),
        cooldowns={("openrouter", "openrouter/free"): NOW + timedelta(minutes=5)},
        today=(("gemini", "g1", UsageTotals(3, 10, 20, 0, NOW)),),
        now=NOW,
    )
    lines = fmt.format_usage(snap).splitlines()
    assert "42 remaining (EXACT)" in lines[0]
    assert "~7 remaining (ESTIMATED)" in lines[1]
    assert lines[2].endswith("UNKNOWN") and not any(ch.isdigit() for ch in lines[2])
    assert any("cooling until 21:05 UTC" in ln for ln in lines)
    assert any(ln.startswith("Today gemini/g1: 3 requests") for ln in lines)


def test_usage_no_data():
    snap = UsageSnapshot(quota=(), cooldowns={}, today=(), now=NOW)
    assert fmt.format_usage(snap) == "No quota data (UNKNOWN)"


def test_models():
    listings = (
        ModelListing("gemini", "g1", ModelRoutingStatus.ALLOWED, "gemini-free", 0),
        ModelListing("gemini", "g2", ModelRoutingStatus.ALLOWED, "gemini-free", 1),
        ModelListing("gemini", "x", ModelRoutingStatus.DISCOVERED_ONLY, None, None),
        ModelListing("gemini", "y", ModelRoutingStatus.DISCOVERED_ONLY, None, None),
    )
    caps = {"g1": ModelCapability("gemini", "g1", supports_tool_calling=True, context_window=1000)}
    out = fmt.format_models([ProviderModels("gemini-free", "gemini", listings, caps)])
    assert "1. g1 — ALLOWED — tools, ctx=1000" in out
    assert "2. g2 — ALLOWED — caps unknown" in out
    assert "2 other models DISCOVERED_ONLY (never selectable)" in out


def test_status_variants():
    running = RunningTask("abcdef12-1", "s", "openrouter", "m", 2, "ses_abcdefghijk", NOW)
    ok = StatusSnapshot(ExecutorHealth(True, "1.0", "ok"), running, 3, (task(),))
    out = fmt.format_status(ok)
    assert "PC: online" in out and "OpenCode: healthy (1.0)" in out
    assert "Current task: abcdef12 openrouter/m attempt 2" in out and "Queue: 3" in out
    bad = StatusSnapshot(ExecutorHealth(False, None, "ConnectError"), None, 0, ())
    out = fmt.format_status(bad)
    assert "OpenCode: unreachable (ConnectError)" in out and "Current task: none" in out


def test_sessions_and_cancel():
    s = SessionRecord(
        "abcdef12-3", "ses_0123456789abcdef", "default", SessionStatus.OPEN, NOW, NOW, None
    )
    assert fmt.format_sessions([s]) == "abcdef12 default OPEN oc=ses_01234567"
    assert fmt.format_sessions([]) == "No sessions."
    assert "cancelled" in fmt.format_cancel(CancelResult.CANCELLED, "abc123")
    assert "not found" in fmt.format_cancel(CancelResult.NOT_FOUND, "abc123")
