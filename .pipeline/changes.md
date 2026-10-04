# Changes: key-holding LLM gateway + controller-managed `opencode serve` (feat/llm-gateway)

Nothing committed or pushed. Branch unchanged. Run everything with `.venv/Scripts/python`.

Result: `pytest -q` 1062 passed, 1 xfailed (pre-existing strict xfail); `ruff check .` and `ruff format --check .` clean; `-m app --check --config config.example.yaml` prints `config OK`.

## Deviations / decisions to review

- `--once` also starts the managed server (spec wiring has no `once` exception). With the default `managed_server: true` and no `opencode` binary, `--once` exits 3. `tests/test_cli.py::test_once_subprocess` now sets `opencode.managed_server: false`; a new test asserts the exit-3 message.
- Gateway reads upstream bodies via `aiter_bytes()` (httpx-decoded) instead of `aiter_raw()`, and sends `Accept-Encoding: identity`, so a relayed body never carries a stale `Content-Encoding`. Stream passthrough is still byte-identical (tested with 7-byte chunks).
- `x-request-id` on usage rows is taken from the UPSTREAM response (`x-request-id` / `request-id`), clipped to 100 chars.
- `check_opencode_health(url, ..., auth=)` keeps its `server_url: str` signature; the manager and executor pass `endpoint.url` and `endpoint.auth()`.
- Gateway catches `ConnectionResetError` only for client disconnect (`aiohttp.ClientConnectionResetError` subclasses it on aiohttp >= 3.10, and `>=3.9` is the pin).
- Test infra: `tests/conftest.py` has a new autouse fixture that points `XDG_DATA_HOME` at a tmp dir (never read the developer's real `auth.json`). `tests/oc_helpers.py` builds the "good" `GET /config` body from `build_opencode_config`. `tests/test_opencode_server.py` overrides the autouse no-network guard (loopback fake server only).
- Windows note for the shell tool: heredocs containing apostrophes/backslashes were unreliable, so some files were written with the Write tool. No effect on repo content.

## Source files

New:
- `app/gateway/__init__.py`, `app/gateway/server.py`: `LlmGateway` (aiohttp on 127.0.0.1). Bearer-token auth first (`hmac.compare_digest`, before reading body); POST `/v1/chat/completions`, GET `/v1/models`, catch-all 404 (also wrong methods). Pipeline: body size/JSON checks (413/400) -> model must be a key of `config.gateway_models()` else 403 `model_not_allowlisted` -> cooldown check (429 + `Retry-After`, no upstream call, no usage row) -> adapter + `chat_headers()` else 503 `upstream_credential_missing` -> body whitelist (21 keys) + `model` rewrite, dropped key NAMES logged -> POST to `adapter.chat_url()` only (no client headers forwarded, `follow_redirects=False`, `trust_env=False`) under a semaphore. Success: stream passthrough with line-buffered usage parsing (1 MiB cap) or capped non-stream body. Non-2xx: capped error body -> `adapter.error_from_response` -> redact (redactor + gateway token) -> same status (3xx -> 502), `Retry-After`, ERROR usage row, cooldown for RATE_LIMITED/QUOTA_EXHAUSTED/PROVIDER_UNAVAILABLE. Transport errors: TRANSIENT_NETWORK 502 (no row), else 504 + UNKNOWN_OUTCOME row; class name only, never exception text. Usage rows are always `CostClass.FREE`, tagged with `current_task_id()`; sink failures logged by class name only.
- `app/opencode/endpoint.py`: `ServerEndpoint(url, password, username)` with `.auth()`; repr hides the password.
- `app/opencode/env.py`: `BASE_ENV_NAMES` + `build_opencode_env` (allowlist + `LC_*` + passthrough + extra). Replaces the old blocklist.
- `app/opencode/auth_store.py`: `auth_store_path`, `stored_credential_providers` (keys only, sanitized, capped), `auth_store_problem`.
- `app/opencode/server.py`: `build_opencode_config` and `OpenCodeServerManager` (preflight on auth store, `resolve_executable`, spawn with allowlisted env + `OPENCODE_CONFIG_CONTENT` / server password, stderr tail, health wait, `_monitor` restart loop with backoff, idempotent `stop`).

Modified:
- `pyproject.toml`: `aiohttp>=3.9,<4`.
- `app/config/models.py`: `GATEWAY_PROVIDER_ID`, `SUPPORTED_PROVIDERS`; `provider_map` removed (before-validator with the exact message); unsupported policy provider -> `PAID_NOT_SUPPORTED`; OpenRouter free rule keyed on provider id; model ids reject `{`, `}`, whitespace; `server_url` http only for local hosts; new `OpenCodeConfig` fields (`managed_server`, `server_port`, `server_directory`, `server_start_timeout_s`, `server_restart_max_backoff_s`, `env_passthrough` with name rules); new `GatewayConfig`; `AppConfig.gateway`, `gateway_models()`, passthrough-vs-secret-env and `gateway.small_model` validation.
- `app/providers/http.py`, `app/providers/gemini.py`: `CHAT_PATH`, `chat_url()`, `chat_headers()`; Gemini `CHAT_PATH = "openai/chat/completions"` (Bearer, not `x-goog-api-key`).
- `app/opencode/config_check.py`: `opencode_model_ids`, `check_server_config` (checks 0-5, key paths only in details), `_bad_model_key` also walks `command`; `_scope_dirs` always to filesystem root; project files with `provider`/`enabled_providers`/`disabled_providers` are UNSAFE; docstring rewritten.
- `app/opencode/executor.py`: new kwargs `endpoint`, `gateway_base_url`, `environ` (removed `bot_token_env`); model is `broke/<provider>/<model>`; env via `build_opencode_env` plus `OPENCODE_SERVER_PASSWORD/USERNAME`; health/abort/config check use endpoint auth; `check_server_config`; executable resolution via `resolve_executable`.
- `app/opencode/process.py`: `resolve_executable`, `spawn_kwargs`. `app/opencode/health.py`, `app/opencode/abort.py`: `auth=` and `trust_env=False`.
- `app/orchestrator/orchestrator.py`: `_server_config_state` (returns `(check, detail)`, OK-only TTL cache); UNSAFE emits `detail="opencode server config not gateway-only"` + `config_detail`; `_record_usage`, `_MAX_USAGE_EVENTS`, usage imports deleted (gateway is the only `usage_events` writer).
- `app/runtime/interfaces.py`: `AgentExecutor.check_server_config`, `StartupError`, `RuntimeService`.
- `app/runtime/daemon.py`: `services=` started after `recover_interrupted()` and before workers/frontend; stopped in reverse after workers are cancelled and before `conn.close()`; failures in stop are logged by class name.
- `app/runtime/wiring.py`: builds adapters -> one shared `CooldownManager` -> gateway (sink writes through `holder` controller conn; current task id from `orchestrator.running`) -> `redactor.add(token)` -> optional manager (`redactor.add(password)`) -> executor -> orchestrator -> controller with `services=[gateway, manager?]`. `Runtime` gains `gateway`, `server_manager`; `close_runtime` also stops the gateway.
- `app/cli.py`: `StartupError` -> message on stderr, return 3. `app/bot/format.py`: new OPENCODE_UNSAFE_CONFIG text (keeps `Offending:`).
- Docs/config: `config.example.yaml`, `docs/SETUP-discord-opencode.md` (Parts B, C4, D2-D5, checklist, troubleshooting), `SPEC.md` (6.6, 9, 11, 13, 16), `ARCHITECTURE.md` (4.6, 11, 12), `TASKS.md` (T003, T036, T037, T071, T072, T082 G1-G8). R1-R4 are stated as written in the spec; R1 names T072 (next round) as the fix.

## Tests

New: `tests/test_gateway.py`, `tests/test_opencode_env.py`, `tests/test_opencode_auth_store.py`, `tests/test_opencode_server.py`, `tests/oc_helpers.py`.
Updated: `test_config.py` (provider_map removal, unsupported provider, braces, passthrough, gateway models/small_model), `test_provider_http_common.py`, `test_opencode_config_check.py` (rewritten), `test_small_model_preflight.py`, `test_project_config_check.py` (parent-above-git-root now UNSAFE, `provider` key UNSAFE, `command`), `test_slice3_safety.py`, `test_opencode_executor.py`, `test_orchestrator.py` (no usage rows from orchestrator), `test_wiring.py`, `test_daemon.py`, `test_cli.py`, `test_bot_format.py`, `fakes/fake_executor.py` (`server_check*`), `fakes/fake_opencode.py` (`serve` mode with scenarios `ok`, `exit_immediately`, `never_healthy`, `crash_after`; `FAKE_OC_CONFIG_OVERRIDE`, `FAKE_OC_ENV_FILE`, `FAKE_OC_SERVE_COUNTER_FILE`).

## What the tester should focus on

Free-only guarantees:
1. Gateway only forwards `config.gateway_models()`: enabled policy, enabled supported provider, listed in `routing.provider_order`. Check 403 for unlisted/disabled/unordered/non-str `model`, and that nothing is substituted.
2. Body whitelist: `models`, `provider`, `plugins`, `route`, `transforms` never reach the upstream. Upstream URL is exactly `adapter.chat_url()` for all three providers; redirects are never followed (302 -> 502, one upstream call).
3. Usage rows are FREE only, written only by the gateway (orchestrator writes none), no double counting.
4. Config rejects every paid door: unsupported policy provider, non-free OpenRouter id, brace/whitespace model ids (inline-config substitution), `provider_map`.
5. Preflight: `enabled_providers == ["broke"]`, provider keys == `{"broke"}`, npm, baseURL, model keys, `small_model`, `model`/`agent`/`mode`/`command` model values; project files may not carry `provider`/`enabled_providers`/`disabled_providers`; chain scanned to filesystem root.
6. Stored credentials block startup (`StartupError`) and every task (`OPENCODE_UNSAFE_CONFIG`).

Secret handling:
1. OpenCode child env is an allowlist: no provider key, bot token, `OPENCODE_CONFIG*`, unknown var. Verify both the served process (fake dumps its env) and the `run --attach` child. Passthrough names rejected if key-like or equal to a provider/bot env name.
2. Real provider key appears only in the upstream `Authorization` header; client Authorization/x-api-key/cookie are never forwarded.
3. Error bodies, logs and exceptions: key, gateway token and server password are redacted; transport errors expose class names only; auth.json values are never read or echoed; request bodies/headers never logged.
4. Gateway and server bind 127.0.0.1; token compared with `hmac.compare_digest` before reading the body.
5. Residual risks R1-R4 are documented, not fixed (R1 needs T072).

## Suggested mutation checks (each should turn tests red)

- `app/gateway/server.py`: drop `or gid not in self.models` in the model check (verified red: 6 failures). Remove the `ALLOWED_REQUEST_KEYS` filter (send the whole body). Replace `adapter.chat_url()` with a client-supplied URL. Set `follow_redirects=True`. Skip `_authorized` or move it after `request.read()`. Stop calling `_scrub`/`error_from_response` redaction. Forward `request.headers`. Record usage with a non-FREE class or in the orchestrator again.
- `app/opencode/env.py`: replace the filter with `if True` (verified red: 4 failures); add `OPENROUTER_API_KEY` to `BASE_ENV_NAMES`.
- `app/opencode/config_check.py`: drop check 1 (`enabled_providers`), check 2 (provider key set), baseURL check, or the `command` walk in `_bad_model_key`; restore git-root truncation in `_scope_dirs`; remove the project-file `provider` key rule; skip `auth_store_problem`.
- `app/opencode/server.py`: pass `os.environ` instead of `build_opencode_env`; omit `OPENCODE_SERVER_PASSWORD`; drop `enabled_providers` from the config; skip the auth-store preflight; remove the restart loop.
- `app/config/models.py`: drop the braces/whitespace rule, the `SUPPORTED_PROVIDERS` check, the passthrough credential-name rule, or the `provider_map` before-validator.
- `app/runtime/daemon.py`: stop services after `conn.close()` or in forward order; start them after the frontend.
- `app/runtime/wiring.py`: build a second `CooldownManager` for the orchestrator; skip `redactor.add(gateway.token)`.

## Exact commands

```
cd C:/Users/jiche/OneDrive/Desktop/Code/broke-ai-coder
.venv/Scripts/python -m pip install -e ".[dev]"
.venv/Scripts/python -m pytest -q
.venv/Scripts/python -m pytest -q tests/test_gateway.py tests/test_opencode_server.py tests/test_opencode_config_check.py tests/test_opencode_env.py tests/test_opencode_auth_store.py tests/test_config.py tests/test_wiring.py tests/test_daemon.py
.venv/Scripts/python -m ruff check .
.venv/Scripts/python -m ruff format --check .
.venv/Scripts/python -m app --check --config config.example.yaml
```

Lifecycle tests spawn `tests/fakes/fake_opencode.py serve` on loopback. If a run is killed mid-test, clean up only processes whose command line contains `fake_opencode.py`.
