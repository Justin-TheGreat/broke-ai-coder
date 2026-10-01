"""Pure parsing of ``opencode run --format json`` output. Never raises.

Assumptions about the OpenCode event stream (isolated here so they are easy to fix):

B1. ``opencode run --format json`` writes one JSON object per stdout line. Each object has
    ``"type"`` (str); the types used are ``step_start``, ``step_finish``, ``text``,
    ``tool_use`` and ``error``. Other types are ignored. The session id is the top-level
    ``"sessionID"`` (fallback: ``part.sessionID``).
B2. ``text`` events carry ``part.text`` (str). ``tool_use`` events carry ``part.tool`` (str),
    ``part.state.status`` (str) and, for the tools edit/write/patch,
    ``part.state.input.filePath`` (str). ``step_finish`` events carry
    ``part.tokens.input`` / ``part.tokens.output`` (ints).
B3. ``error`` events carry ``error: {name, data: {message?, statusCode?, responseHeaders?,
    responseBody?}}``. Known names: ProviderAuthError, APIError, MessageOutputLengthError,
    MessageAbortedError, UnknownError.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from app.providers.base import ErrorClass
from app.providers.http import classify_status
from app.providers.ratelimit import parse_retry_after

SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_-]{0,127}$")
FILE_TOOLS = frozenset({"edit", "write", "patch"})

_MESSAGE_CAP = 300
_TEXT_CAP = 2000

_QUOTA_RE = re.compile(r"quota|resource.?exhausted|insufficient_quota", re.IGNORECASE)
_RATE_RE = re.compile(r"rate.?limit|too many requests|\b429\b", re.IGNORECASE)
_CONTEXT_RE = re.compile(r"context.?(length|window)|maximum context|too many tokens", re.IGNORECASE)
_MODEL_RE = re.compile(r"model.{0,40}not.?found|model_not_found|unknown model", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class OpenCodeError:
    error_class: ErrorClass
    http_status: int | None
    retry_after_s: float | None
    message: str  # unredacted here, <= 300 chars; executor redacts


@dataclass(frozen=True, slots=True)
class ParsedEvent:
    type: str
    session_id: str | None = None
    text: str | None = None
    tool: str | None = None
    tool_status: str | None = None
    file_path: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    error: OpenCodeError | None = None


def _dict(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _str(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _token(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _session(value: object) -> str | None:
    if isinstance(value, str) and SESSION_ID_RE.match(value):
        return value
    return None


def classify_text(text: str) -> ErrorClass:
    if _QUOTA_RE.search(text):
        return ErrorClass.QUOTA_EXHAUSTED
    if _RATE_RE.search(text):
        return ErrorClass.RATE_LIMITED
    if _CONTEXT_RE.search(text):
        return ErrorClass.CONTEXT_TOO_LARGE
    if _MODEL_RE.search(text):
        return ErrorClass.MODEL_UNAVAILABLE
    return ErrorClass.UNKNOWN


def classify_opencode_error(error: object, *, now: datetime) -> OpenCodeError:
    try:
        return _classify(error, now)
    except Exception:
        return OpenCodeError(ErrorClass.UNKNOWN, None, None, "")


def _classify(error: object, now: datetime) -> OpenCodeError:
    if not isinstance(error, dict):
        return OpenCodeError(ErrorClass.UNKNOWN, None, None, "")
    name = error.get("name")
    data = _dict(error.get("data"))
    message = _str(data.get("message"))
    body = _str(data.get("responseBody"))
    text = " ".join(p for p in (message, body) if p is not None)[:_TEXT_CAP]
    short = (message if message is not None else (body or ""))[:_MESSAGE_CAP]

    status_raw = data.get("statusCode")
    status: int | None = None
    if (
        isinstance(status_raw, int)
        and not isinstance(status_raw, bool)
        and 100 <= status_raw <= 599
    ):
        status = status_raw

    retry_after: float | None = None
    for key, value in _dict(data.get("responseHeaders")).items():
        if isinstance(key, str) and key.lower() == "retry-after" and isinstance(value, str):
            retry_after = parse_retry_after(value, now)
            break

    if name == "ProviderAuthError":
        cls = ErrorClass.AUTH_FAILED
    elif name == "MessageOutputLengthError":
        cls = ErrorClass.CONTEXT_TOO_LARGE
    elif status is not None:
        cls = classify_status(status)
        if status == 429:
            cls = (
                ErrorClass.QUOTA_EXHAUSTED
                if classify_text(text) == ErrorClass.QUOTA_EXHAUSTED
                else ErrorClass.RATE_LIMITED
            )
        elif status in (400, 413) and _CONTEXT_RE.search(text):
            cls = ErrorClass.CONTEXT_TOO_LARGE
    else:
        cls = classify_text(text)
    return OpenCodeError(cls, status, retry_after, short)


def parse_event_line(line: str | bytes, *, now: datetime) -> ParsedEvent | None:
    try:
        return _parse(line, now)
    except Exception:
        return None


def _parse(line: str | bytes, now: datetime) -> ParsedEvent | None:
    if isinstance(line, bytes):
        line = line.decode("utf-8", errors="replace")
    line = line.strip()
    if not line:
        return None
    try:
        obj = json.loads(line)
    except ValueError:
        return None
    if not isinstance(obj, dict):
        return None
    etype = obj.get("type")
    if not isinstance(etype, str):
        return None
    part = _dict(obj.get("part"))
    session_id = _session(obj.get("sessionID")) or _session(part.get("sessionID"))

    if etype == "text":
        return ParsedEvent(etype, session_id=session_id, text=_str(part.get("text")))
    if etype == "tool_use":
        state = _dict(part.get("state"))
        tool = _str(part.get("tool"))
        file_path = None
        if tool in FILE_TOOLS:
            file_path = _str(_dict(state.get("input")).get("filePath"))
        return ParsedEvent(
            etype,
            session_id=session_id,
            tool=tool,
            tool_status=_str(state.get("status")),
            file_path=file_path,
        )
    if etype == "step_finish":
        tokens = _dict(part.get("tokens"))
        return ParsedEvent(
            etype,
            session_id=session_id,
            input_tokens=_token(tokens.get("input")),
            output_tokens=_token(tokens.get("output")),
        )
    if etype == "error":
        return ParsedEvent(
            etype,
            session_id=session_id,
            error=classify_opencode_error(obj.get("error"), now=now),
        )
    return ParsedEvent(etype, session_id=session_id)
