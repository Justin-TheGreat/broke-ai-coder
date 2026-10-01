# Review: slice 3 (OpenCode executor, orchestrator, Discord frontend, wiring) + commit 247a167 (free-only)

VERDICT: NEEDS WORK

Branch `feat/discord-opencode` (uncommitted, on top of 247a167). Re-run with `.venv/Scripts/python`:
- `pytest -q`: 719 passed, 1 xfailed.
- `-X dev -W error::ResourceWarning -W error::pytest.PytestUnraisableExceptionWarning`: same, no unclosed resources.
- `ruff check .` clean; `ruff format --check .` 113 files formatted; `-m app --check --config config.example.yaml` OK (schema v2).

Most of the slice matches spec slice 3 and is well built: auth, prompt hygiene, bounded readers, redaction, restart recovery, the NullFrontend fallback, and resource closing all hold up. Three defects must be fixed before SHIP. Each one is a case where the tests are green but the behavior is wrong.

## REQUIRED FIXES

### R1. Free-only loophole through `opencode.provider_map` (new in this slice)
The OpenRouter free check in `app/config/models.py:326` looks at the **policy provider id**. But the executor builds the OpenCode model from `provider_map.get(provider, provider)` (`app/opencode/executor.py:100`). Confirmed with the venv: this config is accepted, and the executor then runs a **paid** OpenRouter model:

    providers: {groq: {api_key_env: GROQ_API_KEY}}
    routing: {provider_order: [g], policies: {g: {provider: groq, model_order: ["openai/gpt-oss-120b"]}}}
    opencode: {provider_map: {groq: openrouter}}
    -> opencode_model = "openrouter/openai/gpt-oss-120b"

Groq really lists `openai/gpt-oss-120b`, so the router marks it eligible. The general problem: `provider_map` can point a policy at any OpenCode provider, and OpenCode holds its own credentials.

Fix:
- In `OpenCodeConfig._provider_map_valid` (`app/config/models.py:247`), allow only the values `{"openrouter", "google", "groq"}`. Require the map to be one-to-one: no two keys may map to the same value.
- In `AppConfig._providers_defined` (`app/config/models.py:322-333`), apply `is_free_openrouter_model` to every policy whose **effective OpenCode provider** (`self.opencode.provider_map.get(pol.provider, pol.provider)`) is `"openrouter"`, not only to policies whose provider id is `"openrouter"`.
- Add config tests for both cases in `tests/test_config.py`.

### R2. An unexpected exception in a task leaves it RUNNING forever (tester observation; must fix)
`Orchestrator.handle` (`app/orchestrator/orchestrator.py:275-292`) catches only `CancelledError`. If `executor.run`, a repository call, or `route_with_state` raises, then:
- the task stays ROUTING or RUNNING, and so does its attempt row;
- no `task_finished` is emitted, so Discord never hears back (the worker's `task_handler_error` event has no task_id or channel);
- `/cancel` returns NOT_CANCELLABLE, because the task is not tracked and not QUEUED (`orchestrator.py:200`);
- `close_idle` never closes the session, because it has an unfinished task;
- the task stays wedged until the next restart.

The worker loop also logs the full traceback with `logger.exception` (`app/runtime/daemon.py:257`), which goes against the "class names only" rule.

Fix:
- In `handle`, add `except Exception as e:` after the `CancelledError` branch. Log `type(e).__name__` only.
- Best effort, move the task from its current status to FAILED with error_class `UNKNOWN`. The outcome is unknown, so do not replay and do not fall back.
- Best effort, finish the open attempt row as FAILED/UNKNOWN. Emit `task_finished` with `session_short`, so the user gets the "Outcome unknown; not replayed. Continue with /code session:" hint.
- Ignore `InvalidTransition`, `ConcurrentTransition`, `TaskNotFound` and `sqlite3.Error` here, and do not re-raise.
- Tighten `tests/test_slice3_safety.py:440-444` to assert `FAILED` with `error_class == "UNKNOWN"`, and assert that a `task_finished` event was emitted.

### R3. Cancel and timeout kill only the `opencode run --attach` client; the server-side session keeps running
With `--attach`, the agent loop and its tools (edit/bash) run inside the OpenCode **server**. Killing the CLI process tree (`executor.py:347-353`) does not stop that work. On Windows, `taskkill /F` leaves the client no chance to clean up. So today:
- `/cancel` reports "Task cancelled" and the row says CANCELLED, while OpenCode keeps editing the repo and using quota. This fails T063 "Running task receives cancellation".
- After a timeout or cancel, the next queued task starts at once in the same repo while the old session may still be writing. This breaks Decision 2 (one task at a time, no concurrent edits).
- The unknown-outcome message tells the user to `/code session:<id>`, which can prompt into a session that is still busy.

Fix:
- In `OpenCodeExecutor.run`, on the cancel, timeout and outer-`CancelledError` paths, after `kill_process_tree`, when `state.session_id` is known, send a best-effort `POST {server_url}/session/{session_id}/abort`. Use an `httpx.AsyncClient(transport=self._transport, timeout=health_timeout_s, follow_redirects=False)` inside `async with`. It must never raise, and must log only the class name.
- In the `CancelledError` path, shield it the same way as the kill.
- Put the endpoint in one constant with a B-assumption note (B11).
- Test it with MockTransport (scenarios `hang` + cancel and `hang` + timeout): assert exactly one abort request to `/session/ses_.../abort`. Also assert that no abort is sent when no session id was seen.
- Document the remaining gap (no session id seen yet means abort is impossible) in the executor docstring.

## Answers to the two extra items

1. **Commit 247a167 (free-only):**
   - `CostClass` has only FREE.
   - `PolicyConfig` rejects `cost_class`, and `RoutingConfig` rejects `mode`/`paid_*`/budget keys. Both run as `mode="before"` validators, and `extra="forbid"` backs them up.
   - `LimitConfig` rejects SPEND_USD.
   - `UsageRepository._validate` rejects any non-FREE value.
   - Migration 2 adds BEFORE INSERT/UPDATE triggers on `tasks`, `provider_model_policy` and `usage_events`. Combined with the v1 CHECK that restricts cost_class to FREE or PAID, this leaves no other value: lowercase or other strings fail the CHECK, and `tasks.cost_class` NULL is harmless.
   - The router evaluates every candidate, returns the first eligible one in configured order, and otherwise returns FREE_CAPACITY_EXHAUSTED.
   - Eligibility still requires allowlist AND provider-discovered capabilities (`app/quota/state.py:137-145`), so models an adapter advertises outside the allowlist are never selectable.
   - The `is_free_openrouter_model` check itself is sound: the model ids are already stripped, and `:free` is case-sensitive, so lookalikes are rejected.
   - The only loophole is R1. It comes from the new `provider_map` key in this slice, not from 247a167 itself.
   - This slice adds no other paid concept. `grep -i paid app/` finds only the free-only guards and the "Nothing was paid for" text. `set_selection` and usage rows write FREE only.
   - Account-side billing (Gemini Cloud Billing, Groq plan, OpenRouter credits) is outside what the code can enforce, and the setup doc covers it.
   - See F1 for an OpenCode-side source of paid calls that still needs checking.
2. **Executor exception wedge:** this must be fixed before SHIP. See R2.

## Verified (no change needed)
- **Discord auth** (`app/bot/auth.py:18-26`):
  - All three lists must match, and an empty list denies everyone. A DM or a None channel is denied.
  - Auth runs before `defer` in `DiscordFrontend._handle` (`discord_frontend.py:163-178`) and again in `run_command`.
  - A denial logs only the command, the user id and the reason, never the options.
- **No blind replay:**
  - A tool event without a session forces UNKNOWN, which is in STOP_ERRORS (`orchestrator.py:660-661`).
  - TIMED_OUT maps to TIMEOUT_UNKNOWN_OUTCOME, and a missing error class becomes UNKNOWN. Both stop the task.
  - Fallback sends `continue_prompt` with `--session <oc_sid>` (`orchestrator.py:675-679`). The original prompt is re-sent only when no session was seen and there were zero tool events.
  - EXECUTOR_UNAVAILABLE and an unhealthy server set no cooldown.
  - Restart recovery marks interrupted tasks FAILED or CANCELLED and never re-enqueues anything.
- **Same session, strict order:** every attempt goes through `route_with_state` with the accumulated FailedAttempt list, so the candidates come only from config `provider_order`/`model_order`.
- **Process tree:**
  - Windows uses `taskkill /T /F` and then `proc.kill()`. POSIX uses `killpg` with TERM then KILL on a new session.
  - The outer CancelledError path shields the kill. Tester mutation 4 confirms the heartbeat tests catch a broken kill.
  - Caveat: see R3.
- **Prompt:**
  - Passed as a single argv element via `create_subprocess_exec` (no shell). A leading `-` gets a space prefix, NUL characters are stripped, and length is capped at 4000.
  - `.cmd`/`.bat` are refused on Windows (`executor.py:301`). stdin is DEVNULL.
- **Bounded memory:**
  - stdout uses `limit=max_line_bytes`, and an oversized line is skipped.
  - The text tail is capped at `output_tail_chars`, the stderr bytearray is trimmed, `changed_files` is capped at 20, and usage events at 500.
  - The Discord message cache is an LRU of 100. The event queue is bounded and progress events are throttled.
- **Secrets:**
  - The token is resolved in `start` through a closure: not in repr/vars, and never stored on self as a string.
  - Login failures log only the class name. Every Discord reply goes through `safe_text` (redaction).
  - The executor redacts the tail, messages and paths. Nothing from output is written to the DB (attempt rows store only class, status and retry_after).
  - Health and errors report class names only. The tester checks on iterdump, events and DEBUG logs pass.
- **NullFrontend:** a missing or blank token logs the env var **name**, and `--once` always uses NullFrontend. discord is imported lazily, so `--check` and `--once` never import it.
- **Resources:** `close_runtime` closes the adapters and the executor. The health check uses `async with`. `DiscordFrontend.stop` closes the client (and with it the aiohttp session) and awaits or cancels the task with a 10 s timeout. ResourceWarning-as-error run is clean.

## Carried-forward follow-ups (ranked)
1. **F1 (verify before T082 live run): OpenCode small_model.** OpenCode makes auxiliary calls (session titles, summaries) with a "small model" it picks from the same provider when `small_model` is unset. On OpenRouter it may pick a paid model, for example an anthropic haiku. With zero OpenRouter credits those calls fail and nothing is charged, but the docs should require pinning `small_model` to a free allowlisted model in opencode.json. Ideally the controller would also check this at startup. Related: limit the OpenCode `enabled_providers` to openrouter/google/groq.
2. **F2:** refuse cmd / cmd.exe / powershell / pwsh as `opencode.command[0]` on Windows. Today `command: [cmd, /c, opencode]` gets past the .cmd refusal and sends the prompt through cmd.exe (confirmed that this config is accepted). It needs an explicit operator choice, but it defeats Decision 7.
3. **F3:** scrub `discord.bot_token_env` (and any provider key env vars OpenCode does not need) from the child env in `executor.py:323`.
4. **F4:** use `SESSION_ID_RE.fullmatch`. The dollar anchor accepts a trailing newline (confirmed: a session id ending in a newline matches). The impact is low, because argv is never shell-parsed.
5. **F5:** the worker logger.exception (`daemon.py:257`) logs full tracebacks. Switch to class-only logging once R2 makes this path rare.
6. **F6:** truncating `output_tail` can cut a secret in half at the front of the tail, and redaction does not catch a partial key. This belongs with the carried slice-2 "partial-key redaction" item.
7. **F7:** the health-check httpx client uses the default trust_env=True. Set trust_env=False so a proxy environment variable cannot route localhost health or abort calls through a proxy.
8. **F8:** RunningTask is created with an empty-string session_id before the session resolves (`orchestrator.py:274`). Use None, or set it after resolution.
9. **F9:** any authorized user can `/cancel` or continue any task or session. That is fine for a single-owner bot. Revisit if multi-user support is added.
10. Deferred per spec section 13 (still open): approvals T060/T061 and /approve, /deny, /attach; start scripts T101/T102 and live E2E T082; OpenCode server Basic auth; B4 internal 429 visibility; quota refresh scheduler T044; concurrent tasks.
11. Slice-2 follow-ups still relevant: snapshot max-age (before T044); 504 precedence in http.py; ratelimit header overflow clamps; partial-key and other-scheme redaction (see F6); install_redaction for handlers added later; A9 Groq per-model header scoping; probe-cost reuse; test_cli redaction-filter hygiene; slice-1 test strengthening.
12. Delete the remaining strict xfail if it is the paid-spend one. It is moot now that PAID rows cannot exist.

**Dropped as moot because of the free-only change:** paid-spend fail-closed and NaN budget checks, the paid_spend missing-cost guard, paid approval flow (PaidApprovalRequired, /approve for paid), PAID_BUDGET_EXHAUSTED and budget-window semantics, and the estimated_cost_usd validation follow-ups.
