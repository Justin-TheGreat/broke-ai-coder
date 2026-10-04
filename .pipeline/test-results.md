# Test results: Required 1(c), crash-to-relaunch window

Status: ALL PASS. No application code was changed by the tester.

## Gate (`.venv/Scripts/python`)
- `pytest -q`, run twice: 1186 passed, 1 xfailed (pre-existing strict xfail), both runs (about 87 s each). The timing tests were not flaky.
- `ruff check .`: clean. `ruff format --check .`: 134 files already formatted.

## New file: tests/test_managed_crash_window_e2e.py (3 tests)
Real Orchestrator, OpenCodeExecutor and OpenCodeServerManager against the fake serve, on loopback only. The task goes through AgentController. The Discord text comes from `format_event`.
1. `test_task_in_crash_backoff_window_fails_closed`: 30 s backoff and a cached server-config OK.
   - The cache is None right after the crash is detected.
   - A foreign server takes the port. Two tasks are submitted.
   - Each task ends FAILED with OPENCODE_UNAVAILABLE, with no attempt row, no cooldown and no `opencode run` spawn (the argv file is absent).
   - The foreign server gets 0 hits, and the serve spawn count stays at 1.
   - The text contains "OpenCode server is not reachable." and "managed OpenCode server is down", with no password or key.
2. `test_task_after_fatal_error_fails_closed`: the port is taken after the crash, so the monitor gives up with a fatal_error. Even with a stale cached OK, two tasks fail the same way. The text includes "already in use". There are 0 foreign hits and no relaunch.
3. `test_task_runs_again_after_successful_relaunch`: after the relaunch (serve count 2, ready) a real task SUCCEEDED, and the executor spawned exactly once.

## Mutation checks (byte-exact backups, restored, verified with `cmp`)
| # | Mutation | Result |
|---|---|---|
| 1 | Remove the exit-path `_notify_launch()` in `_monitor_loop` | FAILED: `test_task_in_crash_backoff_window_fails_closed` and `test_crash_window_clears_cache_and_health_is_unhealthy` |
| 2 | Never clear the ready flag (exit path, `_reap`, fatal setter) | FAILED: both e2e window and fatal tests, plus the other readiness tests. The run hit my 110 s timeout, but the e2e failures were already reported. |
| 3 | Executor ignores readiness (`if False and ...` in `health_check`) | FAILED: both e2e window and fatal tests, `test_managed_executor_unhealthy_until_manager_ready` and `test_crash_window_clears_cache_and_health_is_unhealthy` |

server.py, executor.py and wiring.py were confirmed identical to their backups with `cmp`. No stray fake_opencode processes remain.

An earlier abortive attempt at mutation 2 (a mutation that also broke `__init__`) hung. I killed only that pytest run and its fake_opencode children by PID, restored the file and re-ran correctly.

## Residual notes
- The e2e tests import helpers from `tests/test_opencode_server.py` (`lifecycle_config`, `grab_port` and others). The helpers are used as they are.
- The tests prime the orchestrator cache directly (`_server_config_ok_at`). The fatal-error test is the only place that proves a stale cached OK cannot let a task through.
