# Test results

Status: ALL PASS

Command: `.venv/Scripts/python -m pytest -q` -> 161 passed in about 1.4 s
Lint: `.venv/Scripts/python -m ruff check .` -> All checks passed; `ruff format --check .` -> clean.

Files: tests/test_config.py, test_state_machine.py, test_db_migrations.py, test_task_repository.py,
test_retention.py, test_providers_fake.py, test_router.py, test_router_fallback.py,
test_router_policy.py, test_daemon.py, test_cli.py (extended).

Covered: all spec section 10 cases (router acceptance, paid gating in every mode, fallback chain,
max_fallback_attempts, state machine full transition matrix, migrations/WAL/busy_timeout/schema-too-new,
persistence across reopen, 60-day retention incl. protected tasks/strict cutoff/idempotency/batch_size=1,
config validation failures, secrets absent from repr/json/logs, daemon queues/handlers/stop/start-twice,
CLI check/missing/invalid/--once subprocess).

Note: one test initially failed due to a test-side misuse of the `full_state` helper (duplicate
`credentials_present` kwarg); fixed in the test with `dataclasses.replace`. No application defects found.
