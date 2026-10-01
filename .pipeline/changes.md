# Changes: slice 3 (OpenCode executor, orchestrator, Discord frontend, wiring)

Branch `feat/discord-opencode`, nothing committed. Gate: 674 passed, 1 xfailed; `ruff check .` and `ruff format --check .` clean; `-m app --check --config config.example.yaml` OK. discord.py 2.7.1 installed fine on Python 3.14 (also pulls aiohttp, audioop-lts).

## Modified files
- `pyproject.toml`: added `discord.py>=2.5,<3`.
- `app/config/models.py`: `OpenCodeConfig` extended (command, provider_map, projects, timeouts, tails, continue_prompt, `project_directory()`); new `OrchestratorConfig` on `AppConfig.orchestrator`; `ProviderConfig.base_url` rejects http:// except localhost/127.0.0.1/::1.
- `app/config/secrets.py`: `resolve_secret` returns the stripped value.
- `app/providers/http.py`: `request_id` truncated to 100 chars.
- `app/runtime/interfaces.py`: new `ExecutionOutcome`, `ExecutorEventKind`, `ExecutorEvent`, `ExecutionResult` (replaced), `ExecutorHealth`, `AgentExecutor` (replaced); `ChatFrontend.on_event` (+ `NullFrontend.on_event`).
- `app/runtime/daemon.py`: `TaskSubmission.task_id/session_id`; `orchestrator=` param and property; `start()` binds orchestrator and runs `recover_interrupted()` after migrate; `enqueue_task`; `cancel_task`; `stop()` calls `begin_shutdown()` first; `_event_loop` fans events to `frontend.on_event` with a 10 s timeout and swallows errors; cleanup closes idle sessions (`sessions_closed` field); `metrics()["running_task_id"]`.
- `app/db/tasks.py`: `set_selection`, `attach_session`, `find_by_ref`, `list_recent`.
- `app/db/provider_models.py`: `list_model_ids`.
- `app/cli.py`: non-check path is `asyncio.run(_serve(config, redactor, once=...))`; runtime built inside the loop via `build_runtime`, `close_runtime` in `finally`. `--check` unchanged.
- `config.example.yaml`: new opencode keys (defaults), `orchestrator:` section, allowlist-denies-all comment.
- `docs/SETUP-discord-opencode.md`: removed the `git checkout feat/providers-quota` line.

## New files
- `app/opencode/events.py`: pure JSON-line parser and error classification (assumptions B1-B3 documented in the docstring).
- `app/opencode/process.py`: `kill_process_tree` (taskkill /T /F on Windows, killpg on POSIX).
- `app/opencode/health.py`: `GET /global/health` check (B7).
- `app/opencode/executor.py`: `OpenCodeExecutor` (argv build, bounded stdout/stderr readers, timeout/cancel/outer-cancel handling, redaction, .cmd/.bat refusal).
- `app/orchestrator/prompt.py`: `sanitize_prompt`, `PROMPT_MAX_CHARS` (kept separate to avoid an import cycle; re-exported from `orchestrator.py`).
- `app/orchestrator/orchestrator.py`: `Orchestrator`, `RunningTask`, `CancelResult`; handle loop, fallback by continuing the OpenCode session, unknown-outcome stop, restart recovery, shutdown, throttled progress events.
- `app/db/sessions.py`, `app/db/attempts.py`: `SessionRepository`, `FallbackAttemptRepository`.
- `app/bot/{auth,format,commands,backend,discord_frontend}.py`: authorization (empty lists deny), pure formatting, pure command handlers, `ControllerBackend`, thin discord.py adapter with `create_frontend`.
- `app/runtime/wiring.py`: `Runtime`, `build_runtime`, `close_runtime` (discord imported lazily, only when `once` is False).
- Tests: `tests/fakes/{__init__,fake_opencode,fake_executor}.py`, `test_opencode_events.py`, `test_opencode_executor.py`, `test_opencode_health.py`, `test_sessions_attempts_repo.py`, `test_orchestrator.py`, `test_bot_auth.py`, `test_bot_format.py`, `test_bot_commands.py`, `test_bot_backend.py` (extra), `test_discord_frontend.py`, `test_wiring.py`; additions to `tests/test_config.py` and `tests/test_daemon.py`.

## Tester focus
- Executor on Windows: process tree kill on cancel, timeout and outer `CancelledError` (heartbeat file stops); `.cmd` refusal; oversized-line skipping (`long_line`); memory-bounded `flood`.
- Fallback semantics in `Orchestrator._run_task`: continue-session vs. re-send original prompt vs. stop with `UNKNOWN` (tool events but no session); TIMED_OUT never replayed; EXECUTOR_UNAVAILABLE and unhealthy server never cool down a provider; usage rows all FREE (the last usage row status is `unknown_outcome` for STOP_ERRORS or timeouts).
- Terminal-state paths: cancel while queued/running, shutdown (`DAEMON_SHUTDOWN` for running and queued), restart recovery (`INTERRUPTED_BY_RESTART`); session closed on terminal task when it has no OpenCode id.
- Discord: auth runs before defer; replies redacted/truncated/backtick-neutralized; token never in `repr`/`vars` of `DiscordFrontend`; no test connects to Discord.
- Judgment calls to review: the `attempt_failed` text reads "checking for another free candidate" (spec said not to promise a next one); `task_finished` event field `session_id` carries the OpenCode session id and `session_short` our session id; the `progress` SESSION event is identified by the `opencode_session_id` field; `/code session:` also adopts the session's project.
- Known baseline: the suite has 542 pre-existing tests (not 635 as the spec said) plus 1 xfail (not 2); nothing was removed.

## Commands
```
.venv/Scripts/python -m pip install -e ".[dev]"
.venv/Scripts/python -m pytest -q
.venv/Scripts/python -m ruff check .
.venv/Scripts/python -m ruff format --check .
.venv/Scripts/python -m app --check --config config.example.yaml
.venv/Scripts/python -m pytest -q tests/test_opencode_executor.py tests/test_orchestrator.py
```

## Deferred (not implemented, per spec section 13)
Approvals (T060/T061, `/approve`, `/deny`, `/attach`); start scripts T101/T102 and live end-to-end T082; OpenCode server Basic auth; visibility into OpenCode internal 429 retries (B4); slice-2 review follow-ups (snapshot max-age, 504 precedence in http.py, ratelimit header overflow clamps, partial-key redaction, `install_redaction` for later handlers, A9 Groq header scoping, probe-cost reuse, test_cli redaction-filter hygiene, slice-1 test strengthening); the strict xfail (reviewer may delete the paid-spend one); quota refresh scheduler (T044); concurrent tasks.
