# Changes: slice 2 (provider adapters, cooldowns, quota, redaction)

Branch `feat/providers-quota`. Nothing committed. Result: 366 tests pass, ruff check and format clean, `-m app --check --config config.example.yaml` OK.

## Source files

New:
- `app/providers/errors.py`: `ProviderError` (message stored redacted and truncated to 300; no request/response/headers held), `MalformedResponseError` (UNKNOWN), `MissingCredentialError` (AUTH_FAILED, names env var only).
- `app/providers/ratelimit.py`: `parse_duration`, `parse_number`, `parse_retry_after`, `parse_rate_limit_headers` (EXACT only when remaining parsed; absent headers give no record).
- `app/providers/http.py`: `HttpProviderAdapter` base, `classify_status`, `classify_transport_error`, `apply_metadata`, `positive_int` helper, status/code maps. Key resolved from env per request, sent per-request, never stored. Transport errors are raised outside the `except` block so `__cause__` and `__context__` are both None. `health_check` and `classify_error` never raise.
- `app/providers/openrouter.py`, `gemini.py`, `cerebras.py`, `groq.py`: adapters per spec 4.4 (all base URLs and header-dimension maps are module constants). Gemini sends `x-goog-api-key` header, paginates (max 10 pages, WARNING if truncated), `get_quota()` returns `[]` with no request.
- `app/providers/registry.py`: `ADAPTER_TYPES`, `build_adapters`.
- `app/quota/__init__.py`, `normalize.py` (`DIMENSION_SPECS`, `validate_record`, `normalize`), `cooldown.py` (`CooldownManager`, in-memory), `estimator.py` (`estimate_quota`, never EXACT), `state.py` (`refresh_provider`, `refresh_all`, `build_router_state`, `route_with_state`, `RouterStateError`, `ProviderObservation`).
- `app/db/quota.py` (`QuotaSnapshotRepository`), `app/db/usage.py` (`UsageRepository`, `UsageEvent`, ...), `app/db/provider_models.py` (`ProviderModelRepository`). No schema change.
- `app/redaction.py`: `SecretRedactor`, `RedactingFilter`, `install_redaction` (filter goes on handlers). Does not import `app.providers.*`.

Modified:
- `app/providers/base.py`: added `QuotaDimension`.
- `app/config/models.py`: `ModelMetadataConfig`, `LimitConfig`, `CooldownConfig`; `ProviderConfig` gains `base_url`, `timeout_s`, `models`, `limits`; `AppConfig.cooldown`.
- `app/config/secrets.py`: `collect_secret_values`.
- `app/db/connection.py`: `transaction()` rollback failure is logged and the original exception is re-raised.
- `app/runtime/daemon.py`: `submit_task` returns False (with WARNING) once stopped.
- `app/router/types.py`: `NoEligibleReason.ROUTER_STATE_UNAVAILABLE`.
- `app/cli.py`: installs redaction after `basicConfig`; adds config secrets to the redactor after `load_config`.
- `pyproject.toml`: `httpx>=0.27,<1` (installed into `.venv`).
- `config.example.yaml`: `cooldown:` block; commented-out gemini `limits:`/`models:` examples with placeholder text only.
- `README.md`: updated notes per spec 3.4.

## Tests
New: `test_provider_http_common.py`, `test_provider_openrouter.py`, `test_provider_gemini.py`, `test_provider_cerebras.py`, `test_provider_groq.py`, `test_cooldown.py`, `test_quota_normalize.py`, `test_quota_repository.py`, `test_usage.py`, `test_estimator.py`, `test_router_state.py`, `test_redaction.py`, `test_db_connection.py`. Shared helpers (not collected): `tests/adapter_helpers.py`, `tests/provider_compat_cases.py`.
Modified: `tests/conftest.py` (autouse network guard on `httpx.AsyncHTTPTransport`, `SECRET`, `secret_env()`, `json_response()`, `provider_cfg()`), `tests/test_config.py`, `tests/test_daemon.py`.

## Tester focus
- Secret safety: key never in `str/repr/traceback` of errors, adapter repr, caplog (DEBUG), URLs; `__cause__` and `__context__` both None on transport errors and 401 bodies that echo the key.
- Fail-closed paths: negative or NULL paid cost raises `RouterStateError`, `route_with_state` returns `ROUTER_STATE_UNAVAILABLE`; spend never clamped.
- Estimates: always ESTIMATED/UNKNOWN, never EXACT; missing token counts give UNKNOWN with `remaining=None`; unexpired EXACT suppresses estimate (expiry boundary `reset_at == now` counts as expired).
- Cooldown scope: 429 is `(provider, model)`, PROVIDER_UNAVAILABLE and repeated network failures are `(provider, None)`; later `until` wins; `active(now=until)` is expired and pruned.
- `latest()` picks the newest row first, then drops it if expired (an older row is not resurrected). This is my reading of the spec; confirm it is intended.
- Redaction regex edge cases (`Authorization: Bearer [REDACTED]` is not doubled; quoted header dicts; `?key=` query params).
- Test config: the `_no_real_network` autouse fixture blocks only `AsyncHTTPTransport`; adapter tests use `httpx.MockTransport`.
- Note: `git status` shows `.pipeline/spec.md` as modified; I did not edit it.
- Note: running `-m app --check` creates `data/agent-controller.db` (gitignored).

## Commands (from repo root, Git Bash)
```
.venv/Scripts/python -m pip install -e ".[dev]"
.venv/Scripts/python -m pytest -q
.venv/Scripts/python -m ruff check .
.venv/Scripts/python -m ruff format --check .
.venv/Scripts/python -m app --check --config config.example.yaml
```
