# Re-review: R4 fix round (uncommitted on top of e4e5d31, includes R1-R4)

VERDICT: SHIP

I re-ran everything with `.venv/Scripts/python`:
- `pytest -q`: 761 passed, 1 xfailed.
- `ruff check .`: all checks passed.
- `ruff format --check .`: 115 files already formatted.
- `-m app --check --config config.example.yaml`: OK (schema v2).

R4 is fixed correctly, the tests are meaningful, and I confirmed that with my own mutation runs. Nothing regressed. R1, R2 and R3 stay as approved in the previous round. No required fixes remain.

## REQUIRED FIXES
None.

## R4 verification: FIXED
- `app/opencode/executor.py:365` (cancel/timeout path) and `:370` (shielded outer-CancelledError path) both call `self._abort_session(state.session_id or session_id)`. The id seen on stdout comes first, with the passed resume id as the fallback.
- The resume id has been through `SESSION_ID_RE.match` at `:311`, and `abort_opencode_session` checks it again with `fullmatch` before building any URL. So the fallback cannot inject a path or query.
- Preferring the observed id is correct. For a resume it is the same session, and if OpenCode ever forked a new id, the observed id is the one actually running.
- The module docstring's "Known gap" now covers only brand-new sessions before their first event. That is accurate.
- Tests (`tests/test_opencode_abort.py:246-304`): there are tests for a resumed session before its first event on cancel, timeout and outer cancellation. Each one asserts exactly one POST to `/session/ses_known1/abort`. Another test checks that the observed id (`ses_fake1`) wins over the passed id. `test_no_abort_without_session_id` (session_id=None) remains as the negative case.
- Independent mutation checks (run in a scratch copy, so the repo was not touched):
  1. Reverting both calls to `state.session_id`: the 3 resumed-session tests fail.
  2. Reverting only the outer-cancellation call: `..._outer_cancellation_before_first_event` fails. So each call site is pinned by its own test.
  3. Swapping the precedence to `session_id or state.session_id`: `test_observed_session_id_preferred_over_passed_id` fails.

## Previously approved (unchanged this round)
- **R1** (provider_map paid loophole): FIXED.
- **R2** (an unexpected exception left the task non-terminal): FIXED.
- **R3** (cancel/timeout did not stop the server session): FIXED, and R4 now closes the resume gap.

## Carried-forward follow-ups (ranked, current status)
1. **F1 (open; verify before the T082 live run): OpenCode small_model.** OpenCode makes auxiliary calls (titles, summaries) with an implicit small model, and on OpenRouter it may pick a paid one. The docs should require pinning `small_model` to a free allowlisted model in opencode.json, and ideally the controller would check this at startup. Also limit `enabled_providers` to openrouter/google/groq.
2. **F2 (open):** refuse cmd / cmd.exe / powershell / pwsh as `opencode.command[0]` on Windows. `[cmd, /c, opencode]` gets past the .cmd/.bat refusal.
3. **F3 (open):** scrub `discord.bot_token_env` (and provider key env vars OpenCode does not need) from the child env built in `executor.run` (`{**os.environ, **self._extra_env}`).
4. **F4 (partly resolved; slightly more relevant after R4):**
   - `abort.py` uses `fullmatch`, but `executor.run` (`executor.py:311`) still uses `SESSION_ID_RE.match`, so a resume id with a trailing newline is accepted and passed to `--session`.
   - With R4, such an id is then rejected by the abort's `fullmatch`, so it is silently not aborted.
   - Fix: switch `:311` to `fullmatch`.
5. **F5 (open, rare):** the worker's `logger.exception` (`app/runtime/daemon.py`) logs full tracebacks for failures before `handle`'s `try`. Switch it to class-only logging.
6. **F6 (open):** truncating `output_tail` can cut a secret in half at the front of the tail, and redaction misses partial keys. Track it with the slice-2 partial-key redaction item.
7. **F7 (open):** set `trust_env=False` on the health-check and abort httpx clients, so proxy env vars cannot route localhost calls.
8. **F8 (open):** RunningTask is created with an empty-string session_id (`orchestrator.py`). Use None.
9. **F9 (open):** any authorized user can cancel or continue any task or session. That is fine for a single owner; revisit for multi-user.
10. **F10 (open):** `OpenCodeConfig.server_url` only checks the scheme. Reject non-local `http://`, or require localhost. The OpenCode server has no auth (B7), and prompts, `--attach` traffic and abort calls all go there.
11. **F11 (open, hardening):** pin `provider_map` to the canonical pairs (`openrouter->openrouter`, `gemini->google`, `groq->groq`). A non-canonical one-to-one map is still accepted. It cannot spend OpenRouter credits, but the router then checks a model against one provider's catalogue and runs it on another.
12. **F12 (open, low):** wrap the initial `QUEUED -> ROUTING` transition and the lookups before the `try`. Today a DB error there leaves the row QUEUED until `/cancel` or a restart.
13. **F13 (open, low):** shutdown budget. Kill grace plus the abort timeout can exceed `runtime.shutdown_timeout_s`. Either check this at config time or use a shorter abort timeout during shutdown.
14. **F14 (open, low):** if `executor.run` raises a non-cancel exception after spawning (for example from `kill_process_tree`), the kill and the abort are not retried. Consider a `finally` that ensures the process has exited, plus an abort when the kind is not "exit".
15. **New, informational:** the remaining "Known gap" is a brand-new session cancelled before its first event, which leaves the server-side session unaborted. This is documented. Closing it would need a session listing or creation call through the server API before `run`. Defer it with the server-API work.
16. Deferred per spec section 13 (still open):
    - approvals T060/T061 and /approve, /deny, /attach;
    - start scripts T101/T102 and live E2E T082;
    - OpenCode server Basic auth;
    - B4 internal 429 visibility;
    - quota refresh scheduler T044;
    - concurrent tasks.
17. Slice-2 follow-ups (still open):
    - snapshot max-age (before T044);
    - 504 precedence in http.py;
    - ratelimit header overflow clamps;
    - partial-key and other-scheme redaction (see F6);
    - install_redaction for handlers added later;
    - A9 Groq per-model header scoping;
    - probe-cost reuse;
    - test_cli redaction-filter hygiene;
    - slice-1 test strengthening.
18. Delete the remaining strict xfail if it is the paid-spend one. It is moot now that PAID rows cannot exist.

**Resolved:** R1, R2, R3, R4.
**Dropped earlier as moot because of the free-only change:** paid-spend fail-closed and NaN budget checks, the paid_spend missing-cost guard, the paid approval flow, PAID_BUDGET_EXHAUSTED and budget windows, and the estimated_cost_usd validation items.
