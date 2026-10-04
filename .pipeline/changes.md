# Changes: review Required 1(c), crash-to-relaunch window

## Files
- `app/opencode/server.py`
  - `_monitor_loop`: as soon as `proc.wait()` returns (and we are not stopping) it clears the ready flag and calls `_notify_launch()`, before `_reap()` and the backoff sleep. The orchestrator's cached server-config OK is therefore dropped at the crash, not only at relaunch.
  - New readiness flag (`ready` property, `unavailable_reason()`): set True only at the end of `_launch` after `_wait_healthy` succeeded (authenticated 200, anonymous 401, child alive). Cleared on exit detection, in `_reap`, in `stop()`, and whenever `fatal_error` is set (`fatal_error` is now a property whose setter clears the flag).
  - `unavailable_reason()` returns None when ready, else `managed OpenCode server is down: <fatal_error>` or `...: restarting`. The fatal text is the existing port-in-use message; no secrets.
- `app/opencode/executor.py`: optional `unavailable_reason` callable. `health_check()` returns unhealthy with that reason, without any HTTP request, when it is non-None.
- `app/runtime/wiring.py`: in managed mode passes `manager.unavailable_reason` to the executor (non-managed mode unchanged).
- `app/bot/format.py`: for OPENCODE_UNAVAILABLE, if the task detail starts with `managed OpenCode server is down`, it is appended under the existing "OpenCode server is not reachable." line.
- Tests: `tests/test_opencode_server.py` (3 new), `tests/test_wiring.py` (1 new), `tests/test_bot_format.py` (extended).

## New tests
- `test_crash_window_clears_cache_and_health_is_unhealthy`: 30 s backoff; after the crash the listener fired (cache invalidated), `ready` False, a foreign server grabbed the port gets 0 requests, health is unhealthy, nothing spawned.
- `test_fatal_error_keeps_health_unhealthy_with_reason`: after fatal_error health stays unhealthy with the reason, foreign server 0 hits.
- `test_tasks_run_again_after_successful_relaunch`: after relaunch `ready` and health are true again.
- `test_managed_executor_unhealthy_until_manager_ready` (wiring) and the format assertions.

## Suggested mutation checks
1. Remove the exit-path `_notify_launch()` in `_monitor_loop`: `test_crash_window_...` fails (cache_valid stays True).
2. Never clear the ready flag (drop the `_ready = False` on exit / `_reap` / fatal setter): `test_crash_window_...` times out waiting for `not m.ready`; fatal test fails.
3. Executor ignores readiness (drop the check in `health_check` or the wiring argument): the crash-window and wiring tests fail (health healthy against the foreign server, which then receives requests).

## Tester focus
Timing tests use fixed-port crash_after (1.5 s) with waits up to 20 s and a 30 s initial backoff in the window test; teardown `stop()` cancels the sleeping monitor.
