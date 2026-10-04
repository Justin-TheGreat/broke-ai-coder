# Spec: key-holding LLM gateway + controller-managed `opencode serve` (branch feat/llm-gateway)

Goal: OpenCode never holds a real provider API key. All model traffic goes to a gateway inside
the controller daemon. The gateway holds the keys and forwards ONLY allowlisted free models.

Run everything with `.venv/Scripts/python` (pytest, `-m ruff check .`, `-m ruff format --check .`,
`-m app --check --config config.example.yaml`). No real network in tests.

## OPEN QUESTIONS — RESOLVED by the user on 2026-10-04

All three: implement the stated defaults exactly. Q1: default now (no separate-user code; docs stop sourcing secrets.env from ~/.bashrc and load it only in the daemon start command; document residual risk R1); running OpenCode as a dedicated Linux user (T072) is planned for the NEXT round, so mention it as the planned fix for R1. Q2: pin only `model` and `small_model`. Q3: `managed_server: false` is tests/debug only.

### Original questions (for reference)

1. **Same-OS-user leak (important, needs the user's decision).** The OpenCode agent's shell runs
   as the same Linux user as the daemon. It can therefore still read
   `~/.config/broke-ai-coder/secrets.env` and `/proc/<daemon pid>/environ`. Removing keys from
   the daemon's own `os.environ` does not help: /proc shows the original environment block.
   - This change makes OpenCode's config and environment key-free. It is not yet a guarantee
     against a deliberately rogue agent.
   - Closing the gap needs OpenCode to run as a separate OS user with no read access to the
     secrets file or the daemon's /proc (T072).
   - Default here: no code for that. Document it as residual risk R1 (section 9). In the docs,
     stop sourcing secrets.env from `~/.bashrc`; load it only in the daemon's start command.
     Account-level no-billing stays a required second layer.
2. **Agent model pinning.** The decision asked to pin the default agent models. That is NOT done
   here: if `agent.build.model` takes precedence over `opencode run --model` (unverified), it
   would break routing.
   - Free-only still holds: `enabled_providers: ["broke"]` limits every resolvable model to the
     gateway provider, and the gateway rejects anything not allowlisted.
   - Subagents without a model inherit the invoking agent's model (OpenCode docs).
   - Pinned: `model` and `small_model` only. Revisit at T082.
3. **`opencode.managed_server: false`.** Default meaning: the daemon does not launch `serve` and
   uses `opencode.server_url` without a password. The gateway still runs and the preflight
   (section 6) still requires the gateway-only config. The gateway token is random per run, so
   an externally started server cannot pass the preflight.
   - Net effect: the flag is for tests and debugging only. Document it as unsupported for
     production.

## Assumptions (record in module docstrings; verify at T082)

- G1. `@ai-sdk/openai-compatible` sends `Authorization: Bearer <options.apiKey>` and calls
  `POST {baseURL}/chat/completions`. Streaming is SSE (`data: {json}` lines and
  `data: [DONE]`). The final chunk may carry `usage` (`prompt_tokens`, `completion_tokens`).
- G2. Gemini's OpenAI-compatible endpoint is `{gemini base_url}/openai/chat/completions`, i.e.
  `https://generativelanguage.googleapis.com/v1beta/openai/chat/completions`, with
  `Authorization: Bearer <key>`. Its error bodies may not match the native shape; classification
  then falls back to the status code.
- G3. OpenRouter and Groq: `{base_url}/chat/completions` with Bearer auth.
- G4. `opencode run --attach` authenticates to a password-protected server using
  `OPENCODE_SERVER_PASSWORD` / `OPENCODE_SERVER_USERNAME` from its own environment.
- G5. The `limit` field of a custom-provider model is optional.
- G6. Stored credentials live at `$XDG_DATA_HOME/opencode/auth.json`, defaulting to
  `~/.local/share/opencode/auth.json`. The file is a JSON object keyed by provider id.
- G7. `GET /config` returns the merged config, including `enabled_providers` and `provider`.
  `OPENCODE_CONFIG_CONTENT` outranks global and project files. Managed or MDM system configs
  outrank it (residual risk R3).
- G8. `{env:` / `{file:` substitution also applies to `OPENCODE_CONFIG_CONTENT`. For that reason
  model ids containing `{` or `}` are rejected at config load.

## 1. Dependency

`pyproject.toml`: add `"aiohttp>=3.9,<4"` to `dependencies`. 3.14.3 is installed.

## 2. Config (`app/config/models.py`, tests in `tests/test_config.py`)

- Add `GATEWAY_PROVIDER_ID = "broke"` and `SUPPORTED_PROVIDERS = frozenset({"openrouter", "gemini", "groq"})`.
  These are the keys of `app/providers/registry.ADAPTER_TYPES`. Do not import the registry; that
  would create an import cycle.
- **Remove `OpenCodeConfig.provider_map`** and `_OPENCODE_PROVIDERS`.
  - Add a `mode="before"` validator to `OpenCodeConfig`. If `provider_map` is present, raise
    `"opencode.provider_map was removed: OpenCode reaches providers only through the built-in gateway"`.
    Copy the pattern from `PolicyConfig._reject_cost_class`.
  - `AppConfig._providers_defined` changes:
    - A policy provider must be in `SUPPORTED_PROVIDERS`. Otherwise raise with `PAID_NOT_SUPPORTED`.
    - The OpenRouter free rule applies when `pol.provider == "openrouter"`.
- `PolicyConfig._models_valid`: also reject any model containing `{`, `}` or whitespace (G8).
- `OpenCodeConfig` changes:
  - `server_url`: when it is `http://`, the hostname must be in `_LOCAL_HOSTS` (folds in F10).
    It is only used when `managed_server` is false.
  - New fields:
    - `managed_server: bool = True`
    - `server_port: int = Field(4096, ge=0, le=65535)`. 0 means pick a free port.
    - `server_directory: str = "data/opencode-server"`. This is the cwd of `serve`; the daemon
      creates it.
    - `server_start_timeout_s: float = Field(30, gt=0, le=300, allow_inf_nan=False)`
    - `server_restart_max_backoff_s: float = Field(60, gt=0, le=600, allow_inf_nan=False)`
    - `env_passthrough: list[str] = []`. Extra env var names passed to OpenCode. Each must match
      `ENV_NAME_PATTERN`. Reject names that:
      - start with `OPENCODE_`, `AWS_` or `AZURE_`;
      - end with `_API_KEY`;
      - equal `GOOGLE_APPLICATION_CREDENTIALS`.
- New `GatewayConfig` with `_CFG`; add it to `AppConfig` as `gateway: GatewayConfig = Field(default_factory=GatewayConfig)`:
  - `port: int = Field(0, ge=0, le=65535)`. 0 means ephemeral.
  - `max_request_bytes: int = Field(16_777_216, ge=65_536, le=67_108_864)`
  - `max_response_bytes: int = Field(33_554_432, ge=65_536, le=134_217_728)`. Non-streaming
    bodies only.
  - `max_error_body_bytes: int = Field(65_536, ge=1024, le=1_048_576)`
  - `upstream_connect_timeout_s: float = Field(10, gt=0, le=60, allow_inf_nan=False)`
  - `upstream_read_timeout_s: float = Field(300, gt=0, le=1800, allow_inf_nan=False)`
  - `max_concurrent_requests: int = Field(4, ge=1, le=64)`
  - `small_model: str | None = None`. A gateway model id. If None, the first gateway model is used.
- `AppConfig` additions:
  - `def gateway_models(self) -> dict[str, tuple[str, str]]`. Ordered map of gateway model id
    `f"{provider}/{model}"` to `(provider, model)`. Built by iterating `routing.provider_order`
    only (the same candidates as `app/router/router.build_candidates`), then each policy's
    `model_order`. Include a model only if:
    - the policy is enabled;
    - the provider exists, is enabled, and is in `SUPPORTED_PROVIDERS`.
  - In the after-validator:
    - every `env_passthrough` name must differ from every provider `api_key_env` and from
      `discord.bot_token_env`;
    - if `gateway.small_model` is set, it must be a key of `gateway_models()`.
  - An empty `gateway_models()` is allowed at load. The gateway then serves no models.
- Tests: provider_map rejected with the message; unsupported policy provider rejected; `{` in a
  model rejected; passthrough rules; small_model not allowlisted rejected; `gateway_models`
  order and exclusion of disabled policy, disabled provider, and policies not in `provider_order`.

## 3. Provider adapters (`app/providers/http.py`, `gemini.py`)

- `HttpProviderAdapter`:
  - `CHAT_PATH: ClassVar[str] = "chat/completions"`.
  - Add `def chat_url(self) -> str`, returning `self.base_url.rstrip("/") + "/" + self.CHAT_PATH`.
  - Add `def chat_headers(self) -> dict[str, str] | None`, returning
    `{"Authorization": f"Bearer {key}"}`, or None when the key is missing.
  - Make `error_from_response` usable by the gateway as is. It is already public.
- `GeminiAdapter`: `CHAT_PATH = "openai/chat/completions"` (G2). Do not override `chat_headers`:
  Bearer is correct for the compat endpoint.
- Tests: add to `tests/test_provider_http_common.py`. Check `chat_url` for all three default
  base URLs and a custom base_url, and `chat_headers` None when the key is missing.

## 4. Gateway: new package `app/gateway/` (`__init__.py`, `server.py`)

```python
class LlmGateway:
    def __init__(self, config: AppConfig, *, adapters: Mapping[str, HttpProviderAdapter],
                 cooldowns: CooldownManager, usage_sink: Callable[[UsageEvent], None],
                 current_task_id: Callable[[], str | None], redactor: SecretRedactor | None = None,
                 transport: httpx.AsyncBaseTransport | None = None, token: str | None = None,
                 clock: Callable[[], datetime] = utcnow) -> None
    token: str            # secrets.token_urlsafe(32) unless given
    models: dict[str, tuple[str, str]]   # config.gateway_models() snapshot at init
    @property base_url -> str             # f"http://127.0.0.1:{port}/v1"; RuntimeError before start
    def build_app(self) -> web.Application
    async def start(self) -> None         # AppRunner + TCPSite("127.0.0.1", config.gateway.port)
    async def stop(self) -> None          # idempotent: runner.cleanup(), close httpx client
```

Implementation rules:
- Bind to `127.0.0.1` only. Read the actual port from `runner.addresses`. Use
  `web.Application(client_max_size=max_request_bytes)`.
- Use one `httpx.AsyncClient` with:
  - `transport=transport`, `follow_redirects=False`, `trust_env=False`;
  - `timeout=httpx.Timeout(connect=c, read=r, write=30, pool=c)`.
- Use an `asyncio.Semaphore(max_concurrent_requests)` around upstream calls.
- **Routes:**
  - `POST /v1/chat/completions`
  - `GET /v1/models`
  - A catch-all `*` on `/{tail:.*}` returning 404. Wrong methods also get 404, not 405.
- Every error body is OpenAI-shaped: `{"error": {"message": str, "type": str, "code": str|None}}`.
- **Auth, checked first and before reading the body.**
  - The `Authorization` header must be `Bearer <token>`. Compare the full header bytes with
    `hmac.compare_digest`.
  - Otherwise return 401 `invalid_api_key`. No upstream call.
- **POST /v1/chat/completions, in order:**
  1. Read the body; too large gives 413. The body must be valid JSON and an object, else 400.
     `model` must be a str key of `self.models`, else 403 `model_not_allowlisted` (a stop error,
     no fallback). Never substitute a model.
  2. If `cooldowns.is_cooling(provider, model)`, return 429 `rate_limited` with `Retry-After`.
     The value is the ceiling of the seconds left, from `cooldowns.active()`, max of the
     `(provider, None)` and `(provider, model)` entries. No upstream call and no usage row.
  3. The adapter is `adapters.get(provider)`. Headers are `adapter.chat_headers()`. If either is
     None, return 503 `upstream_credential_missing`.
  4. Build the upstream body:
     - Copy only these top-level keys: `messages, stream, stream_options, temperature, top_p,
       max_tokens, max_completion_tokens, stop, tools, tool_choice, parallel_tool_calls,
       response_format, seed, presence_penalty, frequency_penalty, logit_bias, logprobs,
       top_logprobs, user, reasoning_effort, n`.
     - Set `model` to the upstream model id.
     - Drop any other key and log its NAMES only, at warning. This blocks OpenRouter `models`,
       `route`, `provider`, `plugins` and `transforms`, which can select other or paid models.
  5. Send the request to `adapter.chat_url()` and nowhere else.
     - Headers: `Content-Type: application/json`, the adapter auth header, and `Accept`
       (`text/event-stream` if `stream` is true, else `application/json`).
     - Forward no client header.
     - Use `client.stream("POST", ...)`.
- **Upstream 2xx, streaming** (`stream` true and the response content-type starts with
  `text/event-stream`):
  - Send an `aiohttp.web.StreamResponse(200)` with `Content-Type: text/event-stream` and
    `Cache-Control: no-cache`.
  - For each `aiter_raw()` chunk, `await resp.write(chunk)`. Backpressure bounds the buffering.
    Nothing is accumulated except a line buffer for usage parsing, capped at 1 MiB; on overflow,
    stop parsing usage.
  - From each `data:` line other than `[DONE]` that parses to a dict with a dict `usage`, take
    `prompt_tokens` / `completion_tokens` (ints >= 0; the last seen wins).
  - Usage status:
    - SUCCESS on normal end;
    - UNKNOWN_OUTCOME if the upstream raises mid-stream or the client disconnects
      (`ConnectionResetError` / `aiohttp.ClientConnectionResetError`). On disconnect, close
      upstream.
- **Upstream 2xx, non-streaming:**
  - Read the body up to `max_response_bytes`. If over, return 502 `upstream_response_too_large`
    and record UNKNOWN_OUTCOME.
  - Otherwise return the same status with `Content-Type: application/json` and the bytes. Parse
    `usage` the same way. Record SUCCESS.
- **Upstream non-2xx, including any 3xx (never followed):**
  - Read up to `max_error_body_bytes` and rebuild
    `httpx.Response(status, headers=upstream.headers, content=capped)`.
  - `err = adapter.error_from_response(...)` (already redacted). Then also apply
    `redactor.redact` and replace the gateway token.
  - Respond with the same status, except 3xx becomes 502. Body:
    `{"error": {"message": err.message, "type": "upstream_error", "code": err.error_code}}`.
    Add `Retry-After: ceil(err.retry_after_s)` when known.
  - Record a usage ERROR row.
  - If `err.error_class` is RATE_LIMITED, QUOTA_EXHAUSTED or PROVIDER_UNAVAILABLE, call
    `cooldowns.record_failure(provider, model, cls, retry_after_s=...)`.
  - Network and timeout classes are not recorded here. The orchestrator records them per attempt,
    which avoids double counting.
- **Transport errors** (`classify_transport_error`):
  - TRANSIENT_NETWORK gives 502 `upstream_unreachable` and no usage row.
  - TIMEOUT_UNKNOWN_OUTCOME / other gives 504 `upstream_timeout` and a usage UNKNOWN_OUTCOME
    row.
  - Never include the exception text; give the class name only.
- On upstream 2xx, call `cooldowns.record_success(provider)`.
- **Usage rows:** `UsageEvent(provider, model=upstream model id, cost_class=FREE, status,
  occurred_at=clock(), task_id=current_task_id(), request_id=x-request-id[:100], tokens)`.
  - Tokens are None when unknown.
  - Wrap `usage_sink` in try/except and log the class name only.
- **GET /v1/models** (auth required): `{"object": "list", "data": [{"id": gid, "object": "model",
  "owned_by": "broke"}]}` in `models` order.
- Logs: never log bodies, headers, the token or keys. Log the method, path, gateway model id,
  status and duration.
- **Tests** (new `tests/test_gateway.py`):
  - Harness: `aiohttp.test_utils.TestServer(gw.build_app())` + `TestClient`; upstream via
    `httpx.MockTransport`; adapters from `build_adapters(..., transport=..., environ={...})`.
  - Cases:
    - 401 when the token is missing or wrong, with zero upstream calls;
    - 403 for a model that is not allowlisted, a disabled policy's model, or a model not in
      `provider_order`;
    - the upstream URL is exact for each of the 3 providers;
    - the upstream `Authorization` is the real key, and the client's Authorization, x-api-key and
      cookie are not forwarded;
    - `model` is rewritten;
    - `models`, `provider`, `plugins` and `route` are dropped;
    - streaming passthrough is byte-identical, with usage parsed from the final chunk and the row
      recorded with the task id;
    - non-stream usage;
    - 429 + Retry-After passed through, with a cooldown set and an ERROR row;
    - a cooling model gives 429 with no upstream call;
    - a 302 upstream gives 502 and is not followed;
    - ConnectError gives 502; ReadTimeout gives 504;
    - an upstream error echoing the key and the token is redacted in the response;
    - a missing key env gives 503;
    - an oversized request gives 413; invalid JSON gives 400;
    - an oversized non-stream response gives 502;
    - `/v1/models` lists only the allowlist;
    - an unknown path or method gives 404;
    - a real `start()` reports host 127.0.0.1 and `stop()` is idempotent.

## 5. Managed OpenCode server

### 5a. `app/opencode/endpoint.py` (new)

```python
@dataclass(frozen=True, slots=True)
class ServerEndpoint:
    url: str
    password: str | None = None
    username: str = "opencode"
    def auth(self) -> httpx.BasicAuth | None
```

### 5b. `app/opencode/env.py` (new): child environment allowlist (replaces `_child_env`)

```python
BASE_ENV_NAMES: frozenset[str]  # PATH HOME USER LOGNAME SHELL LANG LANGUAGE TERM TZ TMPDIR TEMP TMP
  # XDG_CONFIG_HOME XDG_DATA_HOME XDG_CACHE_HOME XDG_STATE_HOME XDG_RUNTIME_DIR
  # SYSTEMROOT WINDIR COMSPEC PATHEXT USERPROFILE APPDATA LOCALAPPDATA PROGRAMDATA, plus any LC_*
def build_opencode_env(environ: Mapping[str, str], *, passthrough: Sequence[str],
                       extra: Mapping[str, str]) -> dict[str, str]
```

- Copy only the allowlisted names and the passthrough names that are present, then apply `extra`.
- Because it is an allowlist, every provider key, the bot token, `OPENCODE_CONFIG`,
  `OPENCODE_CONFIG_DIR` and unknown variables are absent by construction. This also closes F3.
- Tests (new `tests/test_opencode_env.py`):
  - absent: provider key vars, `GOOGLE_GENERATIVE_AI_API_KEY`, `DISCORD_BOT_TOKEN`,
    `OPENCODE_CONFIG`, `OPENCODE_CONFIG_DIR`, `FOO_API_KEY`;
  - present: PATH and the `LC_ALL` / passthrough names;
  - extra overrides.

### 5c. `app/opencode/auth_store.py` (new)

```python
def auth_store_path(environ: Mapping[str, str] | None = None) -> Path   # G6
def stored_credential_providers(environ: Mapping[str, str] | None = None) -> list[str] | None
```

- A missing file gives `[]`.
- Invalid JSON, a non-object, or a read error gives `None`, meaning unsafe/unknown.
- Otherwise return the sorted top-level keys. Never read or return values. Sanitize each key:
  - keep `[A-Za-z0-9._-]`, else `?`;
  - truncate to 40 characters;
  - keep at most 10 keys, then `+N more`.
- Helper `auth_store_problem(environ) -> str | None` returns a user message or None. Messages:
  - `"OpenCode has stored provider credentials for: a, b. Run \`opencode auth logout\` for each; OpenCode must hold no provider keys."`
  - `"OpenCode credential store unreadable: <path>"`
- Tests: new `tests/test_opencode_auth_store.py`, using `XDG_DATA_HOME` pointed at tmp_path.

### 5d. `app/opencode/server.py` (new): `build_opencode_config` + `OpenCodeServerManager`

```python
def build_opencode_config(config: AppConfig, *, gateway_base_url: str, gateway_token: str) -> dict[str, Any]
```

The function returns:

```json
{"$schema": "https://opencode.ai/config.json",
 "enabled_providers": ["broke"],
 "provider": {"broke": {"npm": "@ai-sdk/openai-compatible", "name": "broke-ai-coder gateway",
   "options": {"baseURL": "<gateway_base_url>", "apiKey": "<token>"},
   "models": {"<gid>": {"name": "<gid>", "limit": {"context": N, "output": N}}}}},
 "model": "broke/<first gid>", "small_model": "broke/<gateway.small_model or first gid>",
 "share": "disabled", "autoupdate": false}
```

- `gateway_base_url` already ends in `/v1`.
- Include `limit` only when `providers[p].models[m]` has both `context_window` and
  `max_output_tokens`.
- If `gateway_models()` is empty, raise `StartupError("no enabled allowlisted models; check routing.provider_order/policies")`.

```python
class OpenCodeServerManager:
    def __init__(self, app_config: AppConfig, *, gateway: LlmGateway, redactor: SecretRedactor | None = None,
                 environ: Mapping[str, str] | None = None, password: str | None = None,
                 health_transport: httpx.AsyncBaseTransport | None = None) -> None
    password: str                         # secrets.token_urlsafe(24) unless given
    @property endpoint -> ServerEndpoint  # http://127.0.0.1:<current port>
    async def start(self) -> None
    async def stop(self) -> None          # idempotent
```

- **`start()`:**
  - If `auth_store_problem()` returns a message, raise `StartupError(msg)`.
  - Resolve the executable with the new shared helper `resolve_executable(command) -> str`
    (raises `StartupError`). Move the `shutil.which` and win32 `.cmd`/`.bat` refusal out of
    `executor.run` into `app/opencode/process.py`; the executor keeps returning
    `_unavailable(...)` by catching it.
  - Create `server_directory` with `mkdir(parents=True, exist_ok=True)`.
  - Choose the port: `server_port`, or a free port when it is 0 (bind a socket to 127.0.0.1:0,
    read the port, close it).
  - Spawn `[exe, *command[1:], "serve", "--hostname", "127.0.0.1", "--port", str(port)]` with:
    - `cwd=server_directory`, stdin/stdout DEVNULL, stderr PIPE;
    - `env=build_opencode_env(environ or os.environ, passthrough=..., extra={"OPENCODE_CONFIG_CONTENT": json.dumps(cfg), "OPENCODE_SERVER_PASSWORD": password, "OPENCODE_SERVER_USERNAME": "opencode"})`;
    - process-group kwargs as in `executor.run`.
  - Drain stderr into a bounded tail of `stderr_tail_bytes`.
  - Health-wait: poll `check_opencode_health(endpoint)` every 0.25 s until healthy or
    `server_start_timeout_s`. On timeout or an early exit, kill the tree and raise
    `StartupError("opencode serve did not become healthy: <detail>")`. The detail is the exit
    code and the redacted stderr tail, at most 300 chars.
  - Then start a `_monitor` task.
- **`_monitor`:**
  - Await process exit. If not stopping, log a warning with the exit code and the redacted stderr
    tail.
  - Sleep the backoff (1, 2, 4… capped at `server_restart_max_backoff_s`). Reset the backoff after
    an uptime of at least 60 s.
  - Re-spawn and health-wait; re-pick the port when it is 0. Failed restarts loop until `stop()`.
  - Never raises.
- **`stop()`:**
  - Set stopping and cancel the monitor.
  - `kill_process_tree(proc, grace_s=kill_grace_s)`.
  - Cancel the stderr drain.
- Tests (new `tests/test_opencode_server.py`):
  - Config content, including the `limit` include/omit cases.
  - Lifecycle with the fake serve (section 8):
    - starts healthy;
    - the served env has no provider keys and no bot token, and has
      `OPENCODE_CONFIG_CONTENT` / password;
    - `/global/health` without auth gives 401;
    - after a `crash_after`, it restarts (start counter 2) and the endpoint is healthy again;
    - `stop()` kills it (the port stops answering);
    - `never_healthy` gives StartupError within the timeout;
    - an `auth.json` with keys gives StartupError naming the ids and no values;
    - a missing executable gives StartupError.
  - Use small timeouts and backoff (monkeypatch the 60 s reset constant).

### 5e. HTTP helpers: add auth and `trust_env=False` (folds in F7)

- `check_opencode_health`, `abort_opencode_session` and the config check each take
  `auth: httpx.Auth | None = None` and pass `trust_env=False` to `httpx.AsyncClient`.
- Update the B7 docstring in `health.py`: Basic auth is now supported.

## 6. Preflight (`app/opencode/config_check.py`)

- Replace `allowed_small_models` with `opencode_model_ids(config) -> frozenset[str]`, which is
  `{f"broke/{gid}" for gid in config.gateway_models()}`.
- Replace `check_small_model` with:

```python
async def check_server_config(endpoint: ServerEndpoint, allowed: frozenset[str], *, gateway_base_url: str | None,
                              timeout_s: float, transport=None, environ=None) -> tuple[SmallModelCheck, str]
```

Checks, in order. Each failure returns `(UNSAFE, detail)`, where the detail names the key path
only and never values. `GET /config` failing or non-dict gives `(UNAVAILABLE, "GET /config failed")`.
  0. `auth_store_problem(environ)` gives UNSAFE with that message. Run it off-loop with
     `asyncio.to_thread`.
  1. `enabled_providers` must be exactly the list `["broke"]`.
  2. `provider` must be a dict whose key set is exactly `{"broke"}`.
  3. Within `provider.broke`:
     - `npm == "@ai-sdk/openai-compatible"`;
     - `options.baseURL == gateway_base_url` (skip this check when `gateway_base_url` is None);
     - `models` is a dict whose keys are all in `{a.removeprefix("broke/") for a in allowed}`.
  4. `small_model` must be present and in `allowed`.
  5. `_bad_model_key(body, allowed)` must return None. Extend it to walk `command` like
     `agent`/`mode` (review fix 4).
- **On-disk project check (defense in depth), simplified:**
  - `allowed` is now `opencode_model_ids`.
  - `_scope_dirs` always returns the full chain to the filesystem root (review fix 2). Update the
    B13 docstring.
  - In project files, any `provider`, `enabled_providers` or `disabled_providers` key is UNSAFE
    (`key 'provider' is not allowed in project config`; review fix 4).
  - Markdown agent scanning (review fix 1) is NOT added (superseded, section 10).
- Rewrite the module docstring (B12/B13). The authoritative guarantee is "no keys in OpenCode +
  gateway allowlist"; these checks are accident detection.
- Tests: update `tests/test_opencode_config_check.py`, `tests/test_small_model_preflight.py`,
  `tests/test_project_config_check.py` and `tests/test_slice3_safety.py`.
  - Invert `test_without_git_only_project_dir_checked`: a parent file above a non-git project is
    now UNSAFE.
  - New cases: one per check 0–5, including:
    - `enabled_providers` missing, `["broke","groq"]` and `"broke"` (not a list);
    - an extra provider entry;
    - a wrong baseURL;
    - a non-allowlisted model key under `provider.broke.models`;
    - `command.x.model` paid;
    - a project file with `provider`;
    - Basic auth sent (assert the header on the MockTransport).

## 7. Executor, orchestrator, interfaces, daemon, wiring, CLI, bot text

- **`app/runtime/interfaces.py`:**
  - Rename the `AgentExecutor.check_small_model` protocol method to
    `check_server_config(self, allowed: frozenset[str]) -> tuple[SmallModelCheck, str]`.
  - Add `class StartupError(Exception)`.
  - Add the `RuntimeService` Protocol: `async start() -> None`, `async stop() -> None`.
  - Update `tests/fakes/fake_executor.py`: rename the attributes `small_model*` to
    `server_check*`, and return `(self.server_check, detail)`.
- **`app/opencode/executor.py`:**
  - New ctor kwargs:
    - `endpoint: Callable[[], ServerEndpoint] | None = None`. The default is
      `lambda: ServerEndpoint(config.server_url)`.
    - `gateway_base_url: Callable[[], str | None] | None = None`.
    - `environ: Mapping[str, str] | None = None`.
  - Remove `bot_token_env`; the env allowlist makes it moot.
  - `opencode_model(provider, model)` returns `f"broke/{provider}/{model}"`.
  - `build_argv` uses `--attach endpoint().url`.
  - The child env is `build_opencode_env(...)` with `extra` holding `OPENCODE_SERVER_PASSWORD` /
    `OPENCODE_SERVER_USERNAME` when the password is set (G4), plus `extra_env`.
  - Health, abort and the config check pass `endpoint().auth()`.
  - Update the B5/B13 docstrings.
- **`app/orchestrator/orchestrator.py`:**
  - Use `opencode_model_ids`.
  - Rename `_small_model_state` to `_server_config_state`. It caches only OK, with the same TTL,
    and returns `(check, detail)`.
  - On UNSAFE, emit `detail="opencode server config not gateway-only"` and
    `config_detail=<detail>`. On UNAVAILABLE, keep the current message.
  - **Delete `_record_usage`, `_MAX_USAGE_EVENTS` and the usage imports.** The gateway is now the
    only writer of `usage_events`, which avoids double counting. Update the B10 docstring.
  - Cooldown and attempt logic is unchanged.
- **`app/bot/format.py` (OPENCODE_UNSAFE_CONFIG text):**
  - Replace it with: "OpenCode's config is not limited to the built-in free-model gateway, or
    OpenCode has stored provider credentials, so the task was not run. Remove provider settings
    from opencode.json files and run `opencode auth logout`."
  - Keep the `Offending:` line. Update the matching test in `tests/test_bot_format.py`.
- **`app/runtime/daemon.py`:**
  - `AgentController.__init__` gains `services: Sequence[RuntimeService] = ()`.
  - In `start()`, after `recover_interrupted()` and before workers and the frontend, start the
    services in order and record which ones started.
  - In `_teardown()`, after cancelling the worker tasks and before closing `conn`, stop the
    started services in reverse order. Wrap each in try/except and log the class name only.
  - A failing service start propagates through the existing `except BaseException: _teardown()`.
- **`app/runtime/wiring.py`:**
  - Build these in order:
    1. `adapters`
    2. `cooldowns = CooldownManager(config.cooldown)`
    3. a `holder` for the controller
    4. `gateway = LlmGateway(..., usage_sink=_sink, current_task_id=lambda: orchestrator.running.task_id if orchestrator.running else None)`.
       `_sink` writes `UsageRepository(controller.conn).record(ev)` when conn is not None.
    5. `redactor.add(gateway.token)`
    6. If `managed_server`:
       - `manager = OpenCodeServerManager(config, gateway=gateway, redactor=redactor, environ=environ)`
       - `redactor.add(manager.password)`
       - `endpoint = lambda: manager.endpoint`
       Otherwise `manager = None` and `endpoint` is static.
    7. `executor = OpenCodeExecutor(..., endpoint=endpoint, gateway_base_url=lambda: gateway.base_url, environ=environ)`
    8. `orchestrator = Orchestrator(..., cooldowns=cooldowns)`
    9. `controller = AgentController(..., services=[gateway, *([manager] if manager else [])])`
  - Add `gateway` and `server_manager` fields to `Runtime`.
  - `close_runtime` also awaits `gateway.stop()`; it is idempotent.
  - Tests in `tests/test_wiring.py`:
    - the services order;
    - one shared CooldownManager;
    - the redactor holds the token and the password;
    - `managed_server=false` gives no manager;
    - F17: the env passed to the executor has no bot token.
- **`app/cli.py`:** in `main`, catch `StartupError` before the generic `Exception`. Print
  `str(e)` to stderr and return 3. Add a test in `tests/test_cli.py`.
- **Daemon tests (`tests/test_daemon.py`):**
  - services start before the frontend and stop after the workers, in reverse order;
  - a failing second service stops the first and closes conn.

## 8. Fake `opencode serve` (`tests/fakes/fake_opencode.py`)

- When `sys.argv[1] == "serve"`, parse `--hostname` and `--port` and run a stdlib
  `ThreadingHTTPServer`.
- If `OPENCODE_SERVER_PASSWORD` is set, require Basic auth (`opencode:<pw>`); otherwise answer 401.
- `GET /global/health` returns `{"healthy": true, "version": "fake"}`.
- `GET /config` returns `json.loads(OPENCODE_CONFIG_CONTENT)`, or the `FAKE_OC_CONFIG_OVERRIDE`
  JSON when that is set.
- `POST /session/<id>/abort` returns `true`.
- Write `dict(os.environ)` as JSON to `FAKE_OC_ENV_FILE`, if set.
- Increment the counter in `FAKE_OC_SERVE_COUNTER_FILE`.
- Scenarios come from `FAKE_OC_SERVE_SCENARIO`:
  - `ok` (default);
  - `exit_immediately` (exit 5 after writing `boom` to stderr);
  - `never_healthy` (health returns 503);
  - `crash_after` (exit 7 after `FAKE_OC_SERVE_CRASH_AFTER_S` seconds, only on the first start
    per the counter).
- Tests set `opencode.command = [sys.executable, <path to fake_opencode.py>]` and pass
  `FAKE_OC_*` through `environ=`. The allowlist drops them otherwise, so in tests either add them
  to `env_passthrough` or pass them via `extra`. Prefer `env_passthrough`: `FAKE_OC_*` names
  pass validation.

## 9. Docs

- **`docs/SETUP-discord-opencode.md`:**
  - **Part B:** Gemini needs `GEMINI_API_KEY` only. Remove `GOOGLE_GENERATIVE_AI_API_KEY`.
    State that keys are for the controller only and must never be given to OpenCode.
  - **C4:** the secrets file drops `GOOGLE_GENERATIVE_AI_API_KEY`, which makes the expected
    count 4 lines.
    - Replace the `.bashrc` sourcing with loading it only when the daemon starts:
      `set -a; . ~/.config/broke-ai-coder/secrets.env; set +a` in the controller's shell or start
      command.
    - Explain R1: never `source` it in `.bashrc`.
  - **D2:** OpenCode is given no keys.
    - Run `opencode auth list`. Then run `opencode auth logout` for every listed provider; the
      daemon refuses to start otherwise.
    - Remove any `provider`, `model`, `small_model` or `enabled_providers` entries from
      `~/.config/opencode/opencode.json`.
    - Model ids come from each provider's dashboard or models API, not `opencode models`.
  - **D2b:** `small_model` and `model` are now pinned by the controller through
    `OPENCODE_CONFIG_CONTENT` (optional `gateway.small_model`). There is no manual edit. Project
    files must not contain `provider` / `enabled_providers`, or model keys outside `broke/...`.
  - **D3 smoke test:** remove the manual `opencode run --model groq/...` step; with no keys it
    cannot work. Replace it with: start the daemon, then run `/code` on `/workspace/hello`.
  - **D4/D5:** do NOT run `opencode serve` yourself. The daemon starts it on
    `127.0.0.1:<opencode.server_port>` with a random password and restarts it on crash. Stop any
    tmux `opencode serve` session.
  - **Checklist and Troubleshooting:** update the affected rows (health curl now needs the
    password, so drop it). Add rows for `opencode auth logout` and the StartupError messages.
- **`config.example.yaml`:**
  - Remove the provider_map comment and rewrite the opencode SAFETY comment for the gateway.
  - Add `managed_server`, `server_port`, `server_directory`, `server_start_timeout_s`,
    `server_restart_max_backoff_s`, `env_passthrough: []` and the `gateway:` block with defaults
    and one-line comments.
  - `small_model_check_ttl_s`: change its comment to "cache a passing OpenCode server-config
    check".
- **SPEC.md:**
  - §6.6: add the gateway as the structural layer.
  - §9: `opencode serve` is launched by the daemon, and the model is `broke/<provider>/<model>`.
  - §11 Secrets: keys are held only by the controller process, OpenCode credential storage is
    forbidden, and residual risks R1–R4 are listed.
  - §13: remove "(if enabled)".
  - §16: add the new config keys.
- **ARCHITECTURE.md:** §4.6 (managed serve + gateway flow diagram: OpenCode → gateway
  127.0.0.1 → upstream https); §11 (the boundary: OpenCode is untrusted for keys; the gateway
  holds them; R1); §12 failure modes (serve crash → restart; gateway upstream 429 → cooldown).
- **TASKS.md:**
  - Add T036 "LLM gateway" and T037 "Managed opencode serve" (done in this change).
  - T003: managed by the daemon.
  - T071: done via 127.0.0.1 + Basic auth.
  - T072: required to close R1.
  - T082: add the G1–G8 verification items.
- **Residual risks (state exactly these, SPEC §11 + ARCHITECTURE §11):**
  - **R1:** the agent runs as the same OS user as the daemon and can read the secrets file and
    `/proc/<pid>/environ`. Fix: T072, a separate OS user.
  - **R2:** `opencode` run by the user outside the daemon with their own keys or config is
    outside this guarantee.
  - **R3:** managed or MDM system OpenCode configs outrank inline config (admin-only).
  - **R4:** account-level billing must stay off (OpenRouter $0 balance, Gemini project without
    billing, Groq Free plan) as the second layer.
  - Also: the gateway token and server password are visible to the agent. That is acceptable:
    they only unlock allowlisted free models on localhost.

## 10. Review items (`.pipeline/review.md`) disposition

| Item | Disposition |
|---|---|
| Required fix 1 (markdown agents) | Superseded. Any model outside `broke/*` cannot resolve because of `enabled_providers`, and `broke/*` is enforced by the gateway. |
| Required fix 2 (scan to root) | Done (section 6). |
| Required fix 3 (server-cwd masking) | Superseded. The daemon owns the serve cwd, and inline config carries the safety keys. |
| Required fix 4 (`command`, `provider`, alias, baseURL) | `command` and project-file `provider` are done. Alias and baseURL in GET /config are covered by check 3 and the gateway. |
| F3 residual, F7, F10, F17, F18 (enabled_providers) | Done. |
| F11 | Superseded (provider_map removed). |
| F19 | Done via R1–R4 docs. |
| F20, F24, R-b, R-c, R-d | Superseded: no keys in OpenCode. |
| F21, F22 | Superseded. |
| F23 | Folded into T082 (G1–G8). |
| F5, F6, F8, F9, F12–F16, the strict xfail, B11 known gap, slice-2 follow-ups, other deferred items | Deferred, unrelated to this change. |

## Patterns to copy

- Adapter-style httpx usage and error redaction: `app/providers/http.py`.
- Subprocess spawn, process-group kwargs and tree kill: `app/opencode/executor.py` `run()` and
  `app/opencode/process.py`.
- Bounded stderr tail: `OpenCodeExecutor._read_stderr`.
- Pydantic config with `_CFG`, before-validators and after-validators: `app/config/models.py`.
- MockTransport tests: `tests/test_opencode_executor.py` and `tests/test_opencode_config_check.py`.
- Fake CLI scenarios: `tests/fakes/fake_opencode.py`.
