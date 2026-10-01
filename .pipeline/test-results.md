# Test results: slice 3 (OpenCode executor, orchestrator, Discord frontend, wiring)

Status: ALL PASS. No application code was changed.

- `.venv/Scripts/python -m pytest -q`: 719 passed, 1 xfailed (was 674 + 1 xfail; +45 new tests)
- `.venv/Scripts/python -m ruff check .`: all checks passed
- `.venv/Scripts/python -m ruff format --check .`: 113 files already formatted

## Added (tests only)
- `tests/test_slice3_safety.py` (45 tests), `tests/fakes/fake_opencode.py` (two new scenarios: `stderr_flood`, `stderr_secret`).

Coverage added on top of the coder's tests:
- Authorization: full 2x2x2 empty/configured matrix (allowed only when all three lists are non-empty and match); ids not interchangeable between lists; DM denied; default config denies. Through `DiscordFrontend._handle` with fake interactions and a real `ControllerBackend`: 5 unauthorized variants (wrong user/guild/channel, DM, no channel) across `/code`, `/status`, `/sessions`, `/cancel` create zero tasks, never call the executor, never defer, only send an ephemeral "Not authorized.", and the prompt is not logged. Authorized path defers first, then follows up.
- Missing/blank token -> NullFrontend, daemon starts and completes a task; `start()` without token creates no client. A login failure log contains only the exception class, never the token (client start stubbed, no network); token absent from repr/vars; event text redacted before send.
- Fallback: 5 exhausted attempts persisted in order, no candidate twice, order follows policy, all on the same task session with same OpenCode session id and continue prompt, cooldown per failed model, retry-after drives cooldown length, FREE_CAPACITY_EXHAUSTED present on the `task_finished` event and rendered as the explicit "No free capacity ... Nothing was paid for" text. Adapters advertising extra non-allowlisted ("paid") models: only allowlisted pairs are ever executed, all usage rows and tasks FREE. UNKNOWN exit class stops with no replay and `unknown_outcome` usage. Tool activity with no session stops with the "not replayed" hint. Cancel between attempts stops before the next candidate. An executor exception does not wedge the worker.
- Real executor through orchestrator: tool-without-session not replayed (1 spawn); 429 without session re-sends the original prompt without `--session`; hang with `run_timeout_s=1` gives TIMEOUT_UNKNOWN_OUTCOME with 1 spawn.
- Executor: timeout kills the whole tree (heartbeat freezes); pre-set cancel still kills; stderr bounded and tail kept; shell metacharacter prompt passed verbatim and no side effect file; `.bat` refused (Windows) and never run; `kill_process_tree` on exited process does not raise; raising `on_event` callback does not break the run; stderr secret redacted.
- Restart recovery: ROUTING, RUNNING, WAITING_APPROVAL -> FAILED; QUEUED -> CANCELLED (all INTERRUPTED_BY_RESTART); finished task untouched; nothing executed, no attempts/usage rows; second start recovers nothing.
- Secrets: provider key echoed by the fake CLI never appears in the full DB dump (`iterdump`), controller events, or DEBUG logs.

## Regression (mutation) checks, each restored byte-exact (`cmp` against backup, no diff)
1. `app/bot/auth.py`: empty user list allowed -> `test_bot_auth.py::test_all_empty_denies` and the new matrix test FAILED as expected. Restored.
2. `app/orchestrator/orchestrator.py`: tool-without-session guard disabled (blind replay) -> `test_tool_activity_without_session_stops_unknown` and the new recorded-hint test FAILED. Restored.
3. `app/orchestrator/orchestrator.py`: TIMED_OUT mapped to RATE_LIMITED (replay on timeout) -> `test_timed_out_is_not_replayed` and `test_real_timeout_is_unknown_outcome_and_not_replayed` FAILED. Restored.
4. `app/opencode/process.py`: Windows kill short-circuited -> 4 tree-kill tests FAILED (the 2 existing, 2 new). Restored. Orphaned heartbeat child processes left by this mutation were killed afterwards.

## Observations (not failures)
- `tests/test_slice3_safety.py::test_executor_exception_does_not_wedge_worker` asserts only that the worker survives and the next task succeeds. The task whose executor raised is left in a non-terminal or failed state (the assertion allows RUNNING/FAILED/CANCELLED). The orchestrator does not catch an unexpected exception from `executor.run` and mark the task terminal itself; the Reviewer may want to confirm whether the controller's worker loop or restart recovery is meant to clean that up.
- The spec's B-assumption about hanging 429 retries inside OpenCode (B4) is untestable offline beyond the timeout path covered above.
