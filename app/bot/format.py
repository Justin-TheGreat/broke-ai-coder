"""Pure formatting of Discord replies. Every return value is redacted and length-bounded."""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import TYPE_CHECKING

from app.db.sessions import SessionRecord
from app.db.tasks import TaskRecord
from app.providers.base import QuotaConfidence, QuotaRecord
from app.redaction import SecretRedactor
from app.runtime.daemon import ControllerEvent

if TYPE_CHECKING:
    from app.bot.commands import ProviderModels, StatusSnapshot, UsageSnapshot
    from app.orchestrator.orchestrator import CancelResult

DISCORD_MAX_CHARS = 1900
_TRUNC_SUFFIX = "\n…(truncated)"
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
_OUTPUT_BLOCK_CHARS = 1200


def _finalize(text: str, redactor: SecretRedactor | None, limit: int, *, neutralize: bool) -> str:
    text = _ANSI_RE.sub("", text)
    if redactor is not None:
        text = redactor.redact(text)
    if neutralize:
        text = text.replace("```", "`​``")
    if len(text) > limit:
        text = text[: max(limit - len(_TRUNC_SUFFIX), 0)] + _TRUNC_SUFFIX
    return text


def safe_text(text: str, redactor: SecretRedactor | None, limit: int = DISCORD_MAX_CHARS) -> str:
    """Strip ANSI, redact, neutralize triple backticks, truncate."""
    return _finalize(text, redactor, limit, neutralize=True)


def format_ack(
    record: TaskRecord, queue_position: int, *, redactor: SecretRedactor | None = None
) -> str:
    return safe_text(
        f"Task {record.id[:8]} queued (position {queue_position}). Project: {record.project_id}.",
        redactor,
    )


def _s(value: object) -> str:
    return "" if value is None else str(value)


def _finished_text(fields: dict[str, object], task_id: str | None, redactor) -> str:  # noqa: ANN001
    short = _s(fields.get("short_id")) or (task_id or "")[:8]
    status = _s(fields.get("status"))
    error_class = _s(fields.get("error_class"))
    session_short = _s(fields.get("session_short"))
    if status == "SUCCEEDED":
        lines = [f"✅ Task {short} completed"]
        changed = _s(fields.get("changed_files"))
        if changed:
            lines.append("Changed: " + ", ".join(changed.split(",")))
        lines.append(f"Provider: {_s(fields.get('provider'))} / {_s(fields.get('model'))}")
        lines.append("Cost class: FREE")
        text = "\n".join(lines)
        tail = _s(fields.get("output_tail")).strip()
        if tail:
            block = safe_text(tail, redactor, _OUTPUT_BLOCK_CHARS)
            text += f"\n```\n{block}\n```"
        return _finalize(text, redactor, DISCORD_MAX_CHARS, neutralize=False)
    if status == "CANCELLED":
        return safe_text(f"🛑 Task {short} cancelled", redactor)
    lines = [f"❌ Task {short} failed: {error_class or 'UNKNOWN'}"]
    if error_class == "FREE_CAPACITY_EXHAUSTED":
        lines.append(
            "No free capacity left on any configured provider. Nothing was paid for. "
            "Try again after cooldowns/quotas reset (/usage)."
        )
    elif error_class in ("TIMEOUT_UNKNOWN_OUTCOME", "UNKNOWN"):
        hint = "Outcome unknown; not replayed."
        if session_short:
            hint += f" Continue with /code session:{session_short}"
        lines.append(hint)
    elif error_class == "OPENCODE_UNAVAILABLE":
        lines.append("OpenCode server is not reachable.")
    return safe_text("\n".join(lines), redactor)


def format_event(event: ControllerEvent, *, redactor: SecretRedactor | None = None) -> str | None:
    f = dict(event.fields)
    name = event.name
    if name == "provider_selected":
        return safe_text(
            f"Provider: {_s(f.get('provider'))} / {_s(f.get('model'))}"
            f" (attempt {_s(f.get('attempt_no'))})",
            redactor,
        )
    if name == "attempt_failed":
        return safe_text(
            f"Attempt {_s(f.get('attempt_no'))} failed: {_s(f.get('error_class'))}"
            " — checking for another free candidate",
            redactor,
        )
    if name == "progress":
        if f.get("opencode_session_id") is not None:
            return safe_text("OpenCode session started", redactor)
        tool = _s(f.get("tool"))
        steps = _s(f.get("llm_steps") if f.get("llm_steps") is not None else 0)
        if tool:
            return safe_text(
                f"Working… {tool} ({_s(f.get('tool_status'))}), steps {steps}", redactor
            )
        return safe_text(f"Working… steps {steps}", redactor)
    if name == "task_finished":
        return _finished_text(f, event.task_id, redactor)
    return None


def _num(value: float | int | None) -> str:
    if value is None:
        return "?"
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return str(value)


def format_status(snapshot: StatusSnapshot, *, redactor: SecretRedactor | None = None) -> str:
    lines = ["PC: online"]
    h = snapshot.opencode
    if h.healthy:
        lines.append(f"OpenCode: healthy ({h.version or 'unknown version'})")
    else:
        lines.append(f"OpenCode: unreachable ({h.detail})")
    r = snapshot.running
    if r is not None:
        oc = r.opencode_session_id[:8] if r.opencode_session_id else "-"
        lines.append(
            f"Current task: {r.task_id[:8]} {r.provider or '-'}/{r.model or '-'}"
            f" attempt {r.attempt_no} oc={oc}"
        )
    else:
        lines.append("Current task: none")
    lines.append(f"Queue: {snapshot.queue_depth}")
    for t in snapshot.recent[:5]:
        lines.append(
            f"{t.id[:8]} {t.status.value} {t.selected_provider or '-'}/{t.selected_model or '-'}"
        )
    return safe_text("\n".join(lines), redactor)


def _quota_line(r: QuotaRecord) -> str:
    who = r.provider if r.model is None else f"{r.provider}/{r.model}"
    head = f"{who} {r.window} {r.unit.value.lower()}:"
    if r.confidence == QuotaConfidence.EXHAUSTED:
        return f"{head} EXHAUSTED"
    if r.remaining is None or r.confidence in (QuotaConfidence.UNKNOWN, QuotaConfidence.COOLDOWN):
        return f"{head} UNKNOWN"
    if r.confidence == QuotaConfidence.ESTIMATED:
        return f"{head} ~{_num(r.remaining)} remaining (ESTIMATED)"
    return f"{head} {_num(r.remaining)} remaining ({r.confidence.value})"


def format_usage(snapshot: UsageSnapshot, *, redactor: SecretRedactor | None = None) -> str:
    lines: list[str] = []
    lines.extend(_quota_line(r) for r in snapshot.quota)
    for (provider, model), until in sorted(
        snapshot.cooldowns.items(), key=lambda kv: (kv[0][0], kv[0][1] or "")
    ):
        who = provider if model is None else f"{provider}/{model}"
        lines.append(f"{who} cooling until {until.strftime('%H:%M')} UTC")
    for provider, model, totals in snapshot.today:
        tokens = (
            f"{totals.input_tokens} in / {totals.output_tokens} out tokens"
            if totals.events_missing_tokens == 0
            else "tokens partially unknown"
        )
        lines.append(f"Today {provider}/{model}: {totals.requests} requests, {tokens}")
    if not lines:
        lines.append("No quota data (UNKNOWN)")
    return safe_text("\n".join(lines), redactor)


def _caps_summary(cap) -> str:  # noqa: ANN001
    if cap is None:
        return "caps unknown"
    parts: list[str] = []
    if cap.supports_tool_calling:
        parts.append("tools")
    if cap.context_window is not None:
        parts.append(f"ctx={cap.context_window}")
    return ", ".join(parts) if parts else "caps unknown"


def format_models(
    groups: Sequence[ProviderModels], *, redactor: SecretRedactor | None = None
) -> str:
    lines: list[str] = []
    for g in groups:
        lines.append(f"{g.policy_id} ({g.provider})")
        discovered_only = 0
        n = 0
        for listing in g.listings:
            if listing.status.value == "ALLOWED":
                n += 1
                cap = g.capabilities.get(listing.model)
                lines.append(f"{n}. {listing.model} — ALLOWED — {_caps_summary(cap)}")
            else:
                discovered_only += 1
        if discovered_only:
            lines.append(f"{discovered_only} other models DISCOVERED_ONLY (never selectable)")
    if not lines:
        lines.append("No models configured.")
    return safe_text("\n".join(lines), redactor)


def format_sessions(
    sessions: Sequence[SessionRecord], *, redactor: SecretRedactor | None = None
) -> str:
    if not sessions:
        return safe_text("No sessions.", redactor)
    lines = [
        f"{s.id[:8]} {s.project_id} {s.status.value}"
        f" oc={s.opencode_session_id[:12] if s.opencode_session_id else '-'}"
        for s in sessions
    ]
    return safe_text("\n".join(lines), redactor)


def format_cancel(result: CancelResult, ref: str, *, redactor: SecretRedactor | None = None) -> str:
    messages = {
        "CANCELLED": f"Task {ref} cancelled.",
        "CANCELLING": f"Cancelling task {ref}…",
        "NOT_FOUND": f"Task {ref} not found.",
        "NOT_CANCELLABLE": f"Task {ref} is already finished.",
    }
    return safe_text(messages.get(str(result), "Unknown result."), redactor)
