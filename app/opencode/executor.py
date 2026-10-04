"""OpenCode CLI executor (``opencode run --attach``).

Assumptions (see also app/opencode/events.py for B1-B3):

B4. OpenCode may retry 429s internally without emitting an event. The only bound on that
    is ``opencode.run_timeout_s``. A timeout is an unknown outcome.
B5. OpenCode's model argument is ``<opencode_provider>/<model>``. OpenCode splits on the
    FIRST ``/``, so ``openrouter`` + ``openrouter/free`` becomes ``openrouter/openrouter/free``.
B8. With ``--attach`` the process cwd selects the project directory, so cwd = project dir.
B9. ``opencode run`` reads piped stdin when stdin is not a TTY, so stdin is always DEVNULL.
B11. On cancel, timeout or outer cancellation the executor also sends a best-effort
    ``POST {server_url}/session/{id}/abort`` (app/opencode/abort.py), because killing the
    ``--attach`` client does not stop the server-side session.

Known gap: for a brand-new session (no ``--session`` passed), no id is known until the first
event carrying one has been read, so before that the server-side session cannot be aborted
and may keep running after a kill. A resumed session is aborted using the id passed in.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import re
import shutil
import subprocess
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx

from app.config.models import OpenCodeConfig
from app.opencode.abort import abort_opencode_session
from app.opencode.config_check import check_project_config_async, check_small_model
from app.opencode.events import SESSION_ID_RE, OpenCodeError, classify_text, parse_event_line
from app.opencode.health import check_opencode_health
from app.opencode.process import kill_process_tree
from app.orchestrator.prompt import sanitize_prompt
from app.providers.base import ErrorClass
from app.redaction import SecretRedactor
from app.runtime.interfaces import (
    ExecutionOutcome,
    ExecutionResult,
    ExecutorEvent,
    ExecutorEventKind,
    ExecutorHealth,
    SmallModelCheck,
)
from app.timeutil import utcnow

logger = logging.getLogger(__name__)

_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
_MAX_CHANGED_FILES = 20
_MAX_PATH_CHARS = 200
_MESSAGE_CAP = 300


@dataclass(slots=True)
class _RunState:
    tail: str = ""
    truncated: bool = False
    session_id: str | None = None
    tool_events: int = 0
    llm_steps: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    tokens_known: bool = True
    changed_files: list[str] = field(default_factory=list)
    first_error: OpenCodeError | None = None
    oversized_lines: int = 0
    stderr: bytearray = field(default_factory=bytearray)


def _unavailable(detail: str) -> ExecutionResult:
    return ExecutionResult(outcome=ExecutionOutcome.EXECUTOR_UNAVAILABLE, detail=detail)


def _invalid() -> ExecutionResult:
    return ExecutionResult(
        outcome=ExecutionOutcome.FAILED,
        error_class=ErrorClass.INVALID_REQUEST,
        error_message="invalid request",
    )


class OpenCodeExecutor:
    def __init__(
        self,
        config: OpenCodeConfig,
        *,
        redactor: SecretRedactor | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        extra_env: Mapping[str, str] | None = None,
        clock: Callable[[], datetime] = utcnow,
        bot_token_env: str | None = None,
    ) -> None:
        self._config = config
        self._bot_token_env = bot_token_env
        self._redactor = redactor
        self._transport = transport
        self._extra_env = dict(extra_env or {})
        self._clock = clock

    def opencode_model(self, provider: str, model: str) -> str:
        return f"{self._config.provider_map.get(provider, provider)}/{model}"

    def build_argv(
        self,
        executable: str,
        *,
        provider: str,
        model: str,
        prompt: str,
        session_id: str | None,
    ) -> list[str]:
        argv = [
            executable,
            *self._config.command[1:],
            "run",
            "--attach",
            self._config.server_url,
            "--model",
            self.opencode_model(provider, model),
            "--format",
            "json",
        ]
        if session_id is not None:
            argv += ["--session", session_id]
        argv.append(" " + prompt if prompt.startswith("-") else prompt)
        return argv

    async def health_check(self) -> ExecutorHealth:
        return await check_opencode_health(
            self._config.server_url,
            timeout_s=self._config.health_timeout_s,
            transport=self._transport,
        )

    async def check_small_model(self, allowed: frozenset[str]) -> SmallModelCheck:
        return await check_small_model(
            self._config.server_url,
            allowed,
            timeout_s=self._config.health_timeout_s,
            transport=self._transport,
        )

    async def check_project_models(
        self, allowed: frozenset[str], project_dir: str
    ) -> tuple[SmallModelCheck, str]:
        return await check_project_config_async(project_dir, allowed)

    async def aclose(self) -> None:
        return None

    async def _abort_session(self, session_id: str | None) -> None:
        await abort_opencode_session(
            self._config.server_url,
            session_id,
            timeout_s=self._config.health_timeout_s,
            transport=self._transport,
        )

    # ---- helpers ---------------------------------------------------

    def _child_env(self) -> dict[str, str]:
        """os.environ minus the Discord bot token and OpenCode config-injection variables.

        Provider keys stay (OpenCode needs them). OPENCODE_CONFIG / OPENCODE_CONFIG_CONTENT are
        removed so no out-of-band config can select a model (B13).
        """
        env = {**os.environ, **self._extra_env}
        for name in ("OPENCODE_CONFIG", "OPENCODE_CONFIG_CONTENT"):
            env.pop(name, None)
        if self._bot_token_env:
            env.pop(self._bot_token_env, None)
        return env

    def _clean(self, text: str) -> str:
        text = _ANSI_RE.sub("", text)
        return self._redactor.redact(text) if self._redactor is not None else text

    def _emit(self, on_event: Callable[[ExecutorEvent], None] | None, event: ExecutorEvent) -> None:
        if on_event is None:
            return
        try:
            on_event(event)
        except Exception as e:
            logger.warning("executor on_event callback failed: %s", type(e).__name__)

    async def _read_stdout(
        self,
        stream: asyncio.StreamReader,
        state: _RunState,
        on_event: Callable[[ExecutorEvent], None] | None,
    ) -> None:
        limit = self._config.output_tail_chars
        while True:
            try:
                line = await stream.readline()
            except ValueError:
                state.oversized_lines += 1
                continue
            if not line:
                return
            ev = parse_event_line(line, now=self._clock())
            if ev is None:
                continue
            if state.session_id is None and ev.session_id is not None:
                state.session_id = ev.session_id
                self._emit(
                    on_event, ExecutorEvent(ExecutorEventKind.SESSION, session_id=ev.session_id)
                )
            if ev.type == "text" and ev.text:
                combined = state.tail + ev.text + "\n"
                if len(combined) > limit:
                    state.truncated = True
                    combined = combined[-limit:]
                state.tail = combined
            elif ev.type == "tool_use":
                state.tool_events += 1
                if (
                    ev.file_path
                    and ev.file_path not in state.changed_files
                    and len(state.changed_files) < _MAX_CHANGED_FILES
                ):
                    state.changed_files.append(ev.file_path[:_MAX_PATH_CHARS])
                self._emit(
                    on_event,
                    ExecutorEvent(
                        ExecutorEventKind.TOOL,
                        tool=ev.tool[:40] if ev.tool else None,
                        tool_status=ev.tool_status[:20] if ev.tool_status else None,
                    ),
                )
            elif ev.type == "step_finish":
                state.llm_steps += 1
                if ev.input_tokens is None or ev.output_tokens is None:
                    state.tokens_known = False
                else:
                    state.input_tokens += ev.input_tokens
                    state.output_tokens += ev.output_tokens
                self._emit(on_event, ExecutorEvent(ExecutorEventKind.STEP))
            elif ev.type == "error" and state.first_error is None:
                state.first_error = ev.error

    async def _read_stderr(self, stream: asyncio.StreamReader, state: _RunState) -> None:
        keep = self._config.stderr_tail_bytes
        while True:
            chunk = await stream.read(4096)
            if not chunk:
                return
            state.stderr += chunk
            if len(state.stderr) > keep:
                del state.stderr[: len(state.stderr) - keep]

    async def _drain(
        self,
        proc: asyncio.subprocess.Process,
        state: _RunState,
        on_event: Callable[[ExecutorEvent], None] | None,
    ) -> None:
        assert proc.stdout is not None and proc.stderr is not None
        try:
            await asyncio.gather(
                self._read_stdout(proc.stdout, state, on_event),
                self._read_stderr(proc.stderr, state),
            )
        except Exception as e:
            logger.warning("executor output reader failed: %s", type(e).__name__)
        await proc.wait()

    def _result(
        self,
        outcome: ExecutionOutcome,
        state: _RunState,
        *,
        exit_code: int | None,
        error_class: ErrorClass | None = None,
        http_status: int | None = None,
        retry_after_s: float | None = None,
        error_message: str | None = None,
    ) -> ExecutionResult:
        return ExecutionResult(
            outcome=outcome,
            exit_code=exit_code,
            error_class=error_class,
            session_id=state.session_id,
            http_status=http_status,
            retry_after_s=retry_after_s,
            error_message=(
                self._clean(error_message)[:_MESSAGE_CAP] if error_message is not None else None
            ),
            output_tail=self._clean(state.tail)[: self._config.output_tail_chars],
            output_truncated=state.truncated,
            tool_events=state.tool_events,
            llm_steps=state.llm_steps,
            input_tokens=state.input_tokens if state.tokens_known and state.llm_steps else None,
            output_tokens=state.output_tokens if state.tokens_known and state.llm_steps else None,
            changed_files=tuple(self._clean(p) for p in state.changed_files),
        )

    def _log(self, task_id: str, provider: str, model: str, state: _RunState, r: Any) -> None:
        logger.info(
            "opencode run task_id=%s provider=%s model=%s outcome=%s exit_code=%s"
            " error_class=%s llm_steps=%s tool_events=%s oversized_lines=%s",
            task_id,
            provider,
            model,
            r.outcome,
            r.exit_code,
            r.error_class,
            state.llm_steps,
            state.tool_events,
            state.oversized_lines,
        )

    # ---- run -------------------------------------------------------

    async def run(
        self,
        *,
        task_id: str,
        provider: str,
        model: str,
        prompt: str,
        session_id: str | None,
        project_dir: str,
        cancel_event: asyncio.Event,
        on_event: Callable[[ExecutorEvent], None] | None = None,
    ) -> ExecutionResult:
        clean_prompt = sanitize_prompt(prompt)
        if clean_prompt is None:
            return _invalid()
        if session_id is not None and not SESSION_ID_RE.fullmatch(session_id):
            return _invalid()

        executable = shutil.which(self._config.command[0])
        if executable is None:
            return _unavailable("opencode executable not found")
        if sys.platform == "win32" and Path(executable).suffix.lower() in (".cmd", ".bat"):
            return _unavailable("refusing .cmd/.bat launcher; set opencode.command")
        if not Path(project_dir).is_dir():
            return _unavailable("project directory missing")

        argv = self.build_argv(
            executable, provider=provider, model=model, prompt=clean_prompt, session_id=session_id
        )
        kwargs: dict[str, Any]
        if sys.platform == "win32":
            kwargs = {
                "creationflags": subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
            }
        else:
            kwargs = {"start_new_session": True}
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=project_dir,
                env=self._child_env(),
                limit=self._config.max_line_bytes,
                **kwargs,
            )
        except OSError as e:
            return _unavailable(type(e).__name__)

        state = _RunState()
        grace = self._config.kill_grace_s
        drain = asyncio.create_task(self._drain(proc, state, on_event))
        cancel_waiter = asyncio.create_task(cancel_event.wait())
        kind = "exit"
        try:
            done, _ = await asyncio.wait(
                {drain, cancel_waiter},
                timeout=self._config.run_timeout_s,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if drain in done:
                kind = "exit"
            elif cancel_waiter in done:
                kind = "cancel"
            else:
                kind = "timeout"
            if kind != "exit":
                await kill_process_tree(proc, grace_s=grace)
                await self._abort_session(state.session_id or session_id)
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(drain, timeout=grace + 5)
        except asyncio.CancelledError:
            await asyncio.shield(kill_process_tree(proc, grace_s=grace))
            await asyncio.shield(self._abort_session(state.session_id or session_id))
            raise
        finally:
            cancel_waiter.cancel()
            if not drain.done():
                drain.cancel()
            await asyncio.gather(cancel_waiter, drain, return_exceptions=True)

        exit_code = proc.returncode
        if kind == "cancel":
            result = self._result(ExecutionOutcome.CANCELLED, state, exit_code=exit_code)
        elif kind == "timeout":
            result = self._result(
                ExecutionOutcome.TIMED_OUT,
                state,
                exit_code=exit_code,
                error_class=ErrorClass.TIMEOUT_UNKNOWN_OUTCOME,
            )
        elif state.first_error is not None:
            err = state.first_error
            result = self._result(
                ExecutionOutcome.FAILED,
                state,
                exit_code=exit_code,
                error_class=err.error_class,
                http_status=err.http_status,
                retry_after_s=err.retry_after_s,
                error_message=err.message,
            )
        elif exit_code == 0:
            result = self._result(ExecutionOutcome.SUCCEEDED, state, exit_code=0)
        else:
            stderr_text = bytes(state.stderr).decode("utf-8", errors="replace")
            result = self._result(
                ExecutionOutcome.FAILED,
                state,
                exit_code=exit_code,
                error_class=classify_text(stderr_text),
                error_message=stderr_text[-_MESSAGE_CAP:],
            )
        self._log(task_id, provider, model, state, result)
        return result
