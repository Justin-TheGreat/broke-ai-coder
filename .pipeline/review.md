# Final review: key-holding LLM gateway + managed `opencode serve` (feat/llm-gateway, uncommitted on 801ecc8)

VERDICT: SHIP

The application code for Required 1(c) was approved last round. The one required test-only fix (the suite could hang) is now done correctly. Nothing in this round blocks.

## Required fix from the last review: VERIFIED
Files: tests/test_opencode_server.py (`grab_port` at 471-484, `test_fatal_error_keeps_health_unhealthy_with_reason` at 524-558) and tests/test_managed_crash_window_e2e.py (`test_task_after_fatal_error_fails_closed` at 166-188).
- **No more unbounded hang:** `grab_port(port, timeout=10.0)` has a monotonic loop-time deadline and raises AssertionError("port N never freed ..."). A lost race now fails fast. `wait_for` was already bounded at 20 s.
- **The race is removed in practice:** both tests now use `_INITIAL_BACKOFF_S = 3.0`, up from 0.1. The child exits at about 1.5 s. The foreign bind is polled every 0.05 s right after `not m.ready`, so it gets about 3 s of headroom before the relaunch. The relaunch then hits the pre-spawn port check, giving fatal_error and no spawn. With no backoff jitter, the fatal path fires about 3 s after exit, well inside the 20 s `wait_for`.
- **`assert serve_count(tmp_path) == 1` right after `grab_port`:** a relaunch that won the race would be reported clearly instead of producing a misleading failure later.
- **Nothing is weakened:** every assertion is unchanged.
  - Unit test: health unhealthy 3 times, with "managed OpenCode server is down" and "already in use" in the detail, no password, not ready, 0 foreign hits, serve count 1.
  - E2E test: a stale cached OK is primed after the fatal error, then 2 tasks fail closed with no attempts, no cooldowns, no argv, 0 foreign hits, the Discord text includes "already in use", serve count 1.
  - Only the timing changed. Other tests that use `grab_port` (the crash-window tests, with a 30 s backoff) just gain the deadline.

## Gate, re-run by the reviewer with `.venv/Scripts/python`
- **Mutation 3** (executor ignores readiness: `if reason is not None:` changed to `if False:` in `OpenCodeExecutor.health_check`), run on the two fixed tests: **2 failed in 10.56 s.**
  - The captured log shows the intended path was reached: serve exited, then "cannot restart ... port N already in use". So the failure came from the health/fail-closed assertions, not from `grab_port`.
  - executor.py was restored from a backup and checked byte-exact with `cmp`.
- **Mutations 1 and 2** were checked by reasoning:
  - (1) The exit-path notify is exercised by the crash-window tests, whose 30 s backoff is unchanged.
  - (2) If ready is never cleared, `wait_for(not m.ready)` in both fatal tests times out at 20 s and fails.
  - The backoff change does not affect either one.
- **Full suite:** `timeout 280 .venv/Scripts/python -m pytest -q`: **1186 passed, 1 xfailed in 88.91 s, exit 0, no hang.** This matches the lead's two runs.
- **ruff check / ruff format --check** on both test files: clean.
- **No leftover fake serve processes** after the runs.

## Required 1(c): verified correct (application code)
- **(a) Notify at exit** (`app/opencode/server.py:356-362`): right after `proc.wait()` returns and `not _stopping`, the code sets `_ready = False` and calls `_notify_launch()`, before `_reap()` and the backoff sleep. `_spawn` still notifies at each launch attempt (`:253`).
  - Wiring (`app/runtime/wiring.py:75-77`) points the listener at `Orchestrator.invalidate_server_config_cache`.
- **(b) Readiness flag** (`server.py:146-173`, `:207-210`, `:241-242`, `:342-346`):
  - Set only in `_launch`, after `_wait_healthy` returned. That requires authenticated 200, anonymous exactly 401 and the child alive, and `_launch` re-checks `proc.returncode is None`.
  - Cleared on exit detection, in `_reap` (which covers every failure and restart path, including the generic-exception path), in `stop()` and in the `fatal_error` setter.
  - `fatal_error` is never reset, so after a fatal error the manager stays not-ready until the daemon restarts (see N10).
- **Executor** (`app/opencode/executor.py:150-156`): when `unavailable_reason()` returns a reason, `health_check()` returns unhealthy with no HTTP request at all, so the password is never sent to whatever holds the port.
  - Order check in `orchestrator.py:500-515`: health comes before `_server_config_state()`. A not-ready manager therefore also prevents the authenticated GET /config to a foreign listener.
  - The task fails OPENCODE_UNAVAILABLE with no attempt row and no cooldown.
- **Reason text**: fixed strings plus the port-in-use message, capped at `_DETAIL_CAP`, with no secrets. `format.py` appends it only when it starts with "managed OpenCode server is down" and passes it through `safe_text` (redaction). This closes N8: fatal_error is now visible to users.
- **Non-managed mode**: `unavailable` stays None, so behaviour is unchanged. `--once` builds no manager.

## Tests: meaningful
- E2E (`tests/test_managed_crash_window_e2e.py`) uses a real Orchestrator, OpenCodeExecutor, OpenCodeServerManager and AgentController, the fake serve, and a foreign server on the fixed port.
  - Window test: a 30 s backoff keeps the run inside the window, and a primed cached OK is dropped at the crash.
  - It submits two tasks. Each must end FAILED with OPENCODE_UNAVAILABLE, with no attempt rows, no cooldowns, no `opencode run` spawn (no argv file), 0 foreign hits and serve count 1.
  - It also checks the Discord text, and that no password or key appears in it.
  - Fatal test: the cached OK is primed after the fatal error, so this proves a stale OK cannot let a task through. The relaunch test proves the gate reopens.
- Mutation table (`test-results.md`): each of the three mutations hits the right tests.
  - (1) Without the exit-path notify, the window tests fail on the cache assertion.
  - (2) If ready is never cleared, they fail or time out.
  - (3) If the executor ignores readiness, the e2e, unit and wiring tests fail.
  - I checked these against the code and the assertions; they are sound.
- Minor: the tests prime `_server_config_ok_at` directly instead of through a real GET /config. That is acceptable, because the fake serve's /config path is covered elsewhere.
- No regressions: everything else in the suite passes, ruff is clean, and the rest of the 801ecc8 round (1(a), 1(b), Required 2, S1-S3) is unchanged in this diff.

## Residuals found this round (not blocking)
- **N9 (new, low):** fallback attempts inside one task do not re-check readiness.
  - `health_check()` runs once per task (`orchestrator.py:500`).
  - `executor.run()` (`executor.py:350`) spawns `opencode run --attach` with the server password in env, without checking readiness.
  - If our serve crashes during attempt k, and attempt k ended with a fallback-eligible class (for example a rate-limit event already seen), attempt k+1 can attach while the manager is not ready.
  - A crash with no prior error event classifies as UNKNOWN, which is in STOP_ERRORS, so it does not fall back. That leaves a ms-scale gap that also requires a foreign bind.
  - Cheap fix: in `run()`, return `_unavailable(reason)` before the spawn when `unavailable_reason()` is non-None.
- **N10 (new, low):** once `fatal_error` is set the monitor returns, so the managed serve stays down until the daemon restarts.
  - The user-facing text says to stop any manually started serve, but not to then restart the daemon.
  - Add that to the message or docs, or add a re-arm path.
- Carried from the last round, unchanged: TOCTOU between the bind test and the spawn; the crafted-server identity gap (R1/T072, plus the F23 apiKey echo check); Windows SO_REUSEADDR (low); the Required 2 bypass hunt results (all OK); N5-N7.

## Follow-ups (carried forward, current status)
1. **F1** (prior required fixes 1-4):
   - Fix 1: SUPERSEDED (enabled_providers ["broke"] plus the gateway).
   - Fix 2: DONE.
   - Fix 3: SUPERSEDED.
   - Fix 4: command and project provider are DONE. Alias/baseURL: SUPERSEDED.
2. **F2, F3, F4: RESOLVED.** The F3 residual is RESOLVED by the env allowlist.
3. **F5 (open, rare):** logger.exception in daemon.py logs full tracebacks.
4. **F6 (open):** output_tail truncation can split a secret, so it escapes redaction.
5. **F7: RESOLVED.**
6. **F8 (open):** RunningTask session_id is an empty string. Use None.
7. **F9 (open):** any authorized user can cancel or continue any task.
8. **F10: RESOLVED.**
9. **F11: SUPERSEDED.**
10. **F12-F16 (open, low):** QUEUED->ROUTING DB error; kill grace vs shutdown timeout; finally for non-cancel exceptions after spawn; monotonic OK-cache clock; command[0] allowlist.
11. **F17: RESOLVED.**
12. **F18: RESOLVED.**
13. **F19: RESOLVED.**
14. **F20, F21, F22: SUPERSEDED.**
15. **F23:** folded into T082 (G1-G8). Still to add:
    - confirm whether GET /config echoes provider.broke.options.apiKey. If it does, compare it to the gateway token as a serve identity check (this also hardens the crafted-server residual);
    - confirm the merge order of enabled_providers.
16. **F24: SUPERSEDED** for keys. The plugin code-execution class remains under R1/T072.
17. **Known gap (open):** a session cancelled before its first event is not aborted server-side.
18. **Deferred (open):**
    - approvals T060/T061 and /approve, /deny, /attach;
    - start scripts T101/T102;
    - live E2E T082;
    - B4 internal 429 visibility;
    - quota refresh T044;
    - concurrent tasks.

    Server Basic auth (T071) is DONE.
19. **Slice-2 follow-ups (open):**
    - snapshot max-age;
    - 504 precedence;
    - ratelimit header clamps;
    - partial-key and other-scheme redaction;
    - install_redaction for late handlers;
    - A9 Groq header scoping;
    - probe-cost reuse;
    - test_cli redaction-filter hygiene;
    - slice-1 test strengthening.
20. **Strict xfail (open):** delete it if it is the paid-spend one. It is moot under free-only.
21. **N1 (open, low):** detached agent subprocesses escape the serve killpg. Verify at T082. A cgroup/systemd scope arrives with T072.
22. **N2 (open, low):** broaden the env_passthrough rejects to *_TOKEN, *_SECRET, *_KEY, *_PASSWORD.
23. **N3 (open, low):** _relay_stream returns an unprepared StreamResponse if prepare() raises. Return a 502 JSON error instead.
24. **N4 (open, R1/T072 list):** the agent holds the serve password and can PATCH /config or PUT /auth/* on serve.
25. **Previous Required 1:** DONE in application code.
    - (a) and (b) were DONE last round.
    - (c) is DONE this round: invalidate at exit, readiness gate, fatal is user-visible.
    - The test hang from the previous review is FIXED: grab_port now has a 10 s deadline, the backoff is 3.0 s and serve_count == 1 is asserted. The reviewer verified it, see above.
26. **Previous Required 2: DONE.**
27. **S1, S2, S3: DONE.**
28. **N5 (open, low):** in the gateway, validate message-level keys and function-tool inner keys with an allowlist. Today they pass through verbatim. No known billing door.
29. **N6 (open, low):** reject a messages[*].content that is not a str, list or None.
30. **N7 (open, low):** reject Groq compound* model ids in config validation, because they run hosted tools server-side. Today they rely on R4.
31. **N8: RESOLVED.** fatal_error now reaches users through executor health and the Discord text.
32. **N9 (new, low):** re-check manager readiness in executor.run() before each attempt's spawn. Fallback attempts inside one task currently skip it.
33. **N10 (new, low):** fatal_error is permanent until the daemon restarts. Tell the user to restart, or add a re-arm path.
34. **R1 (accepted, planned for T072 next round):** the agent runs as the same OS user and can read secrets.env, /proc/<daemon>/environ and ambient cloud credentials (HOME/XDG_*: ~/.aws, gcloud ADC, Azure CLI, Copilot tokens). It is documented and not blocking.

**Dropped earlier as moot because of the free-only change:** the paid-spend, budget and paid-approval items.
