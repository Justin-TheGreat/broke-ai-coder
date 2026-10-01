# Changes: MVP vertical slice 1 (offline core)

Implemented everything in `.pipeline/spec.md` sections 1-9 (no branch switch, no commit).

## Files (all new)
- Tooling: `.gitignore`, `pyproject.toml`, `README.md`, `config.example.yaml`, `scripts/setup_venv.ps1`, `scripts/check.ps1`
- `app/timeutil.py`: utcnow, to_db (rejects naive), from_db, *_opt variants.
- `app/providers/base.py`: enums, error-class partition sets, ModelCapability/QuotaRecord/ProviderHealth, ProviderAdapter Protocol. `app/providers/fake.py`: FakeProviderAdapter, FakeProviderError.
- `app/config/{models,loader,secrets}.py`: Pydantic v2 frozen/extra=forbid config, load_config/parse_config/ConfigError, resolve_secret/credentials_present.
- `app/orchestrator/state_machine.py`: TaskStatus, ALLOWED_TRANSITIONS (MappingProxyType), ensure_transition.
- `app/db/{connection,migrations,tasks,retention}.py`: sqlite3 WAL, transaction(), migration v1 (no executescript), TaskRepository, batched retention cleanup.
- `app/router/{types,router,policy,__init__}.py`: pure router, next_after_failure, allowlist helpers.
- `app/runtime/{interfaces,daemon}.py`: Null frontend, AgentController (bounded queues, worker, event consumer, cleanup loop).
- `app/cli.py`, `app/__main__.py`, `app/__init__.py`.
- Smoke tests only: `tests/conftest.py` (db, make_config fixtures, `full_state(config, now=NOW, **kw)` helper, `NOW`), `tests/test_router.py` (2 tests), `tests/test_cli.py` (2 tests).

## Commands
```
py -3.14 -m venv .venv
.venv\Scripts\python -m pip install -e ".[dev]"
.venv\Scripts\python -m pytest
.venv\Scripts\python -m ruff check .
.venv\Scripts\python -m ruff format --check .
.venv\Scripts\python -m app --check --config config.example.yaml
```
Verified: ruff check/format clean, 4 smoke tests pass, `--check` prints `config OK; schema version 1; database data/agent-controller.db`, `--once` exits 0 in about 0.4 s.

## Tester focus
- Remaining spec section 10 test files are not written (config, state machine, migrations, task repo, retention, fake provider, router fallback/policy, daemon, full router and cli).
- `full_state` in conftest takes `config` as its first arg (spec said `full_state(now)`); import it with `from conftest import full_state, NOW`.
- Router: skip-reason ordering, strict `>` / `<=` boundaries on cooldown and reset_at, paid budget gate, fallback exclusion.
- Retention: protected-task rules, strict `<` cutoff, FK cascade/SET NULL, idempotency, batch_size=1.
- Daemon: bounded queues, stop idempotency, start twice raises, handler exceptions, Windows connection closing (`stop()` closes `conn`; cleanup uses its own connection).
- `--check`/`--once` create `data/` relative to cwd (gitignored); tests should chdir to tmp_path or use a tmp DB path.
- Deviation: `AgentController.stop()` also tears down (cancels tasks, closes DB) if `start()` fails partway.
