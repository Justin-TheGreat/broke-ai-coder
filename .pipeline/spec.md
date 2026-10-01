# Spec: slice 2: provider HTTP adapters, cooldowns, quota, redaction (offline-testable)

Covers T031-T034 (adapters), T035 (cooldowns), T040-T043 (quota normalization, snapshots, usage accounting, estimates), the RouterState assembly, and T070/T073 (secret redaction). It also includes two trivial follow-ups from `.pipeline/review.md`.
Branch: `feat/providers-quota`. Source docs: `SPEC.md`, `ARCHITECTURE.md`, `TASKS.md`. Slice-1 conventions still apply:
- `from __future__ import annotations` at the top of every module.
- Frozen slotted dataclasses.
- `StrEnum`.
- Pydantic `ConfigDict(extra="forbid", frozen=True)` via `_CFG`.
- No `print` outside `app/cli.py`.
- `logging.getLogger(__name__)`.

## OPEN QUESTIONS

None blocking. Every uncertain provider detail is a module-level constant in one place and is listed under "Assumptions" (§0.2). Implement the assumptions as written.

## 0. Environment, decisions, assumptions

### 0.1 Environment
- Use `.venv/Scripts/python` only. Never use the bare `python` on PATH.
- Add `"httpx>=0.27,<1"` to `[project] dependencies` in `pyproject.toml`. Then run `.venv/Scripts/python -m pip install -e ".[dev]"`. httpx is **not** installed yet. Installing needs the network; tests do not.
- Check with `.venv/Scripts/python -m pytest`, `-m ruff check .` and `-m ruff format --check .`. Slice 1 must stay green.
- Tests never touch the network. Every adapter in tests gets `transport=httpx.MockTransport(handler)`. A conftest autouse guard (§10) blocks any real transport.

### 0.2 Assumptions (each one is a constant in one module; change it there only)
| # | Assumption | Where |
|---|---|---|
| A1 | Base URLs: OpenRouter `https://openrouter.ai/api/v1`, Gemini `https://generativelanguage.googleapis.com/v1beta`, Cerebras `https://api.cerebras.ai/v1`, Groq `https://api.groq.com/openai/v1`. Config `base_url` overrides them. Request paths are relative (for example `"models"`). | `DEFAULT_BASE_URL` per adapter |
| A2 | Auth: OpenRouter, Cerebras and Groq use `Authorization: Bearer <key>`. Gemini uses the header `x-goog-api-key: <key>`, **never** a `?key=` query param, so keys never appear in URLs. | `_auth_headers` per adapter |
| A3 | OpenRouter `GET key` returns `{"data": {"limit": number\|null, "usage": number, "limit_remaining": number\|null, ...}}` in USD. No other fields are read. No authoritative remaining **request** count exists, so free-request quota comes only from configured limits through the estimator. | `openrouter.py` |
| A4 | OpenRouter `GET models` returns `{"data":[{"id", "context_length", "top_provider":{"max_completion_tokens"}, "supported_parameters":[...], "architecture":{"input_modalities":[...]}}]}`. `"tools"` in supported_parameters means tool calling. `"structured_outputs"` or `"response_format"` means structured output. `"image"` in input_modalities means vision. `openrouter/free` is assumed to appear in this list. If it does not, it shows as MODEL_UNKNOWN in `skipped`, which is visible and fail-closed. | `openrouter.py` |
| A5 | Gemini `GET models?pageSize=1000[&pageToken=]` returns `{"models":[{"name":"models/<id>", "inputTokenLimit", "outputTokenLimit", "supportedGenerationMethods":[...]}], "nextPageToken"?}`. Strip the `models/` prefix. Keep only models whose methods include `generateContent`; if the field is absent, keep the model. At most 10 pages. Gemini has no remaining-quota API, so `get_quota()` returns `[]` and the estimator supplies ESTIMATED records. | `gemini.py` |
| A6 | Gemini errors use `{"error":{"code","message","status","details":[...]}}`. Error code = `status`. A `details[]` item with `"reason": "API_KEY_INVALID"` means AUTH_FAILED, even on HTTP 400. A details item whose `@type` ends with `google.rpc.RetryInfo` carries `retryDelay` like `"30s"`, which is used as Retry-After when no header is present. | `gemini.py` |
| A7 | Groq headers: `x-ratelimit-{limit,remaining,reset}-requests` mean **requests per day** and `...-tokens` mean **tokens per minute**. Reset values are Go-style durations (`2m59.56s`). | `GROQ_HEADER_DIMENSIONS` |
| A8 | Cerebras headers: `x-ratelimit-{limit,remaining,reset}-requests-day` and `...-tokens-minute`. Reset values are seconds as a decimal string. | `CEREBRAS_HEADER_DIMENSIONS` |
| A9 | Groq and Cerebras `get_quota()` probe `GET models`, which uses no inference. Headers seen on that probe are recorded with `model=None` (provider-wide), because the probe is not model-scoped. If the headers are absent, the result is `[]` (UNKNOWN). Configured limits then feed the estimator, which labels them ESTIMATED. `GET models` on Cerebras returns only ids, so capabilities come from config metadata (§3.1). | `groq.py`, `cerebras.py` |
| A10 | OpenAI-compatible error body is `{"error":{"message","type","code"}}`. Error code = `code` if it is a str, else `type`. | `http.py` |
| A11 | Estimates use **rolling** windows: minute 60 s, hour 3600 s, day 86400 s. Gemini RPD really resets at midnight Pacific. A rolling 24 h window counts at least as much usage as the calendar window, so the estimate is never higher than the truth. Calendar alignment is deferred (no tzdata dependency on Windows). | `WINDOW_SECONDS` in `normalize.py` |
| A12 | Paid spend windows are UTC: "today" starts at UTC midnight and "month" starts at 00:00 UTC on the 1st. | `state.py` |
| A13 | Every usage event counts as one request toward request quotas, whatever its status. This is conservative. | `usage.py` |

### 0.3 Decisions
1. **Adapters only report provider-observed data.** `get_quota()` returns EXACT or UNKNOWN records from provider headers or APIs. ESTIMATED records come only from `app/quota/estimator.py`, which reads configured limits and DB usage. Estimates are computed on demand and **not persisted** in this slice. No numeric limit appears in source. Configured limits come from `providers.<p>.limits`.
2. **Refreshing and building are separate steps.** `refresh_provider_state` calls the adapters over the network, persists snapshots and models, and returns observations. `build_router_state` is synchronous and reads only the DB, the cooldown manager and cached observations. The router never triggers network calls. Scheduling refreshes (T044) is out of scope.
3. **429 cooldowns are model-scoped** (`(provider, model)`), so a Gemini model-1 429 still allows Gemini model-2 (T051A). PROVIDER_UNAVAILABLE and repeated network failures put the whole provider in cooldown (`(provider, None)`).
4. **Keys are resolved from the env at every request** through `resolve_secret(cfg.api_key_env, environ)`. They are never stored on adapters, errors, dataclasses or the DB.
5. **Fail closed on bad state.** `build_router_state` raises only `RouterStateError`; it never lets `ValueError` or `sqlite3.Error` escape. `route_with_state` turns that error into `NoEligibleProvider(ROUTER_STATE_UNAVAILABLE)`.

## 1. Files

Create:
```text
app/providers/errors.py
app/providers/ratelimit.py
app/providers/http.py
app/providers/openrouter.py
app/providers/gemini.py
app/providers/cerebras.py
app/providers/groq.py
app/providers/registry.py
app/quota/__init__.py          (only `from __future__ import annotations`)
app/quota/normalize.py
app/quota/cooldown.py
app/quota/estimator.py
app/quota/state.py
app/db/quota.py
app/db/usage.py
app/db/provider_models.py
app/redaction.py
tests/test_provider_http_common.py
tests/test_provider_openrouter.py
tests/test_provider_gemini.py
tests/test_provider_cerebras.py
tests/test_provider_groq.py
tests/test_cooldown.py
tests/test_quota_normalize.py
tests/test_quota_repository.py
tests/test_usage.py
tests/test_estimator.py
tests/test_router_state.py
tests/test_redaction.py
tests/test_db_connection.py
```
Modify:
```text
pyproject.toml
config.example.yaml
README.md
app/providers/base.py
app/config/models.py
app/config/secrets.py
app/db/connection.py
app/router/types.py
app/runtime/daemon.py
app/cli.py
tests/conftest.py
tests/test_config.py
tests/test_daemon.py
```
Patterns to copy:
- Repositories follow `app/db/tasks.py`: a class taking `conn`, writes in `transaction()`, `to_db`/`from_db`, and a `_row_to_*` helper.
- Config models follow `app/config/models.py`.
- Adapter tests follow the style of `tests/test_providers_fake.py`.
- Parametrized rejection tests follow `tests/test_config.py::test_invalid_rejected`, using `pytest.raises(..., match=...)`.

## 2. `app/providers/base.py` (add only)

```python
class QuotaDimension(StrEnum):
    REQUESTS_PER_MINUTE = "requests_per_minute"
    REQUESTS_PER_HOUR = "requests_per_hour"
    REQUESTS_PER_DAY = "requests_per_day"
    TOKENS_PER_MINUTE = "tokens_per_minute"
    TOKENS_PER_HOUR = "tokens_per_hour"
    TOKENS_PER_DAY = "tokens_per_day"
    SPEND_USD = "spend_usd"
```
It lives here, not in `app/quota`, so that `app/config/models.py` can import it without a cycle.

## 3. Config (`app/config/models.py`, `app/config/secrets.py`), example, README

### 3.1 New models

All use `_CFG`. Every float uses `allow_inf_nan=False`.
```python
class ModelMetadataConfig(BaseModel):        # overlays discovered capability; None = keep discovered value
    supports_tool_calling: bool | None = None
    supports_structured_output: bool | None = None
    supports_vision: bool | None = None
    context_window: int | None = Field(None, ge=1)
    max_output_tokens: int | None = Field(None, ge=1)

class LimitConfig(BaseModel):
    dimension: QuotaDimension             # SPEND_USD -> ValueError ("spend is governed by routing budgets")
    limit: float = Field(gt=0, allow_inf_nan=False)
    model: str | None = None              # None = provider-wide (usage of all models summed); stripped, non-empty if given

class ProviderConfig(BaseModel):          # existing fields unchanged
    enabled: bool = True
    api_key_env: str = Field(pattern=ENV_NAME_PATTERN)
    base_url: str | None = None           # must start with "https://" or "http://" else ValueError
    timeout_s: float = Field(10, gt=0, le=120, allow_inf_nan=False)
    models: dict[str, ModelMetadataConfig] = Field(default_factory=dict)   # keys stripped, non-empty
    limits: list[LimitConfig] = Field(default_factory=list)   # duplicate (model, dimension) -> ValueError

class CooldownConfig(BaseModel):
    rate_limit_default_s: float = Field(60, gt=0)
    provider_unavailable_s: float = Field(120, gt=0)
    network_failure_threshold: int = Field(3, ge=1)
    network_failure_window_s: float = Field(300, gt=0)
    network_failure_cooldown_s: float = Field(120, gt=0)
    max_cooldown_s: float = Field(3600, gt=0)
    # model_validator(after): rate_limit_default_s, provider_unavailable_s, network_failure_cooldown_s
    # must each be <= max_cooldown_s

class AppConfig:  add  cooldown: CooldownConfig = Field(default_factory=CooldownConfig)
```

### 3.2 `secrets.py` (add)
```python
def collect_secret_values(config: AppConfig, environ: Mapping[str, str] | None = None) -> frozenset[str]
    # resolved values of every providers[*].api_key_env plus discord.bot_token_env; unresolved ones are skipped
```

### 3.3 `config.example.yaml`
- Add a `cooldown:` block with the defaults above.
- Under `gemini`, add a **commented-out** example of `limits:` and `models:`, using placeholder text such as `limit: <from your AI Studio dashboard>`.
- Do not put real numbers in the example. The file must still validate.

### 3.4 `README.md`
- Replace the line "Discord, OpenCode, and real provider adapters are not implemented yet." with one that says HTTP provider adapters exist for metadata, health and quota only (no inference calls), and that Discord and OpenCode are not implemented yet.
- Add one line each describing the `providers.<p>.limits`, `providers.<p>.models` and `cooldown` config keys.
- Note that log output redacts configured keys.

## 4. Provider errors and HTTP plumbing

### 4.1 `app/providers/errors.py`
```python
class ProviderError(Exception):
    def __init__(self, provider: str, error_class: ErrorClass, message: str, *,
                 status_code: int | None = None, error_code: str | None = None,
                 retry_after_s: float | None = None, request_id: str | None = None) -> None
    # attributes of the same names; `message` is stored ALREADY REDACTED (caller passes through redactor) and truncated to 300 chars
    @property
    def retry_after_ms(self) -> int | None      # round(retry_after_s*1000) or None
    __str__  -> f"{provider}: {error_class} status={status_code} code={error_code}: {message}"
    __repr__ -> f"ProviderError(provider=..., error_class=..., status_code=..., error_code=...)"
class MalformedResponseError(ProviderError)     # always error_class UNKNOWN
class MissingCredentialError(ProviderError)     # always error_class AUTH_FAILED; message names the env var, never a value
```
These errors never hold an `httpx.Request`, an `httpx.Response`, or any headers.

### 4.2 `app/providers/ratelimit.py`
```python
def parse_duration(value: str | None) -> float | None
    # seconds. Plain non-negative number "12.5" -> 12.5. Go-style sequence of <num><unit>, units h|m|s|ms|us|µs,
    # must consume the whole (stripped) string: "2m59.56s"->179.56, "7.66s"->7.66, "1h2m3s"->3723.0, "120ms"->0.12, "0s"->0.0.
    # "", None, "abc", "-1s", "5x" -> None.
def parse_number(value: str | None) -> int | float | None     # non-negative finite; int if integral text; else None
def parse_retry_after(value: str | None, now: datetime) -> float | None
    # delta-seconds (r"^\d+(\.\d+)?$") -> float; else HTTP-date via email.utils.parsedate_to_datetime
    # (naive -> UTC), seconds = max(0, (dt - now).total_seconds()); anything invalid -> None
def parse_rate_limit_headers(headers: Mapping[str, str], dimensions: Mapping[str, QuotaDimension], *,
                             provider: str, model: str | None, now: datetime) -> list[QuotaRecord]
```
`parse_rate_limit_headers` rules:
- Header lookup is case-insensitive.
- For each `(suffix, dim)` in mapping order, read `x-ratelimit-limit-{suffix}`, `x-ratelimit-remaining-{suffix}` and `x-ratelimit-reset-{suffix}`.
- If both limit and remaining fail to parse, emit no record for that dimension.
- `confidence` is EXACT if remaining parsed, otherwise UNKNOWN.
- `used = limit - remaining` only when both are known and `limit >= remaining`; otherwise `None`.
- `reset_at = now + parse_duration(reset)` when it parses; otherwise `None`.
- `window` and `unit` come from `DIMENSION_SPECS[dim]` (§6.1).
- `source = f"{provider}:response-headers"`.

### 4.3 `app/providers/http.py`: `HttpProviderAdapter` base class

```python
COMMON_STATUS_MAP: Mapping[int, ErrorClass] = {
    400: INVALID_REQUEST, 401: AUTH_FAILED, 403: AUTH_FAILED, 404: MODEL_UNAVAILABLE,
    408: TRANSIENT_NETWORK, 413: CONTEXT_TOO_LARGE, 422: INVALID_REQUEST, 429: RATE_LIMITED,
    500: PROVIDER_UNAVAILABLE, 502: PROVIDER_UNAVAILABLE, 503: PROVIDER_UNAVAILABLE,
    504: TIMEOUT_UNKNOWN_OUTCOME,
}
# other 4xx -> INVALID_REQUEST; other 5xx -> PROVIDER_UNAVAILABLE; anything else -> UNKNOWN
OPENAI_COMPAT_CODE_MAP: Mapping[str, ErrorClass] = {
    "context_length_exceeded": CONTEXT_TOO_LARGE, "model_not_found": MODEL_UNAVAILABLE,
    "rate_limit_exceeded": RATE_LIMITED, "insufficient_quota": QUOTA_EXHAUSTED,
    "invalid_api_key": AUTH_FAILED,
}
REQUEST_ID_HEADERS = ("x-request-id", "request-id")

def classify_status(status: int) -> ErrorClass   # COMMON_STATUS_MAP + fallbacks above
def classify_transport_error(e: BaseException) -> ErrorClass   # check in this order:
    # httpx.ConnectTimeout, httpx.ConnectError, httpx.PoolTimeout, httpx.ProxyError -> TRANSIENT_NETWORK
    # httpx.ReadTimeout, httpx.WriteTimeout, httpx.ReadError, httpx.WriteError, httpx.RemoteProtocolError,
    #   builtin TimeoutError -> TIMEOUT_UNKNOWN_OUTCOME   (request may have been processed)
    # anything else -> UNKNOWN
def apply_metadata(cap: ModelCapability, meta: ModelMetadataConfig | None) -> ModelCapability
    # non-None config values override; returns cap unchanged if meta is None

class HttpProviderAdapter:
    DEFAULT_BASE_URL: ClassVar[str]
    HEALTH_PATH: ClassVar[str]                     # relative, e.g. "models"
    HEALTH_PARAMS: ClassVar[Mapping[str, str]] = {}
    ERROR_CODE_MAP: ClassVar[Mapping[str, ErrorClass]] = {}   # keys lowercase; matched on lowercased code
    STATUS_OVERRIDES: ClassVar[Mapping[int, ErrorClass]] = {}

    def __init__(self, provider_id: str, config: ProviderConfig, *,
                 transport: httpx.AsyncBaseTransport | None = None,
                 environ: Mapping[str, str] | None = None,
                 clock: Callable[[], datetime] = utcnow,
                 redactor: SecretRedactor | None = None) -> None
    def __repr__(self) -> str      # f"{type(self).__name__}(provider_id=..., base_url=...)"; never the key
    async def aclose(self) -> None; async def __aenter__/__aexit__
    async def health_check(self) -> ProviderHealth           # NEVER raises
    async def classify_error(self, error: Exception) -> ErrorClass   # NEVER raises
    def error_from_response(self, response: httpx.Response) -> ProviderError
    # subclass hooks:
    def _auth_headers(self, api_key: str) -> dict[str, str]
    def _parse_error_body(self, body: object) -> tuple[str | None, str, float | None]  # (code, message, retry_after_from_body); default = OpenAI-compatible (A10), retry None
    async def _get_json(self, path: str, params: Mapping[str, str | int] | None = None) -> tuple[object, httpx.Response]
```
Behavior:
- **Client.** The `httpx.AsyncClient` is created lazily on first use with these settings:
  - `base_url=config.base_url or DEFAULT_BASE_URL`
  - `timeout=httpx.Timeout(config.timeout_s)`
  - `transport=transport`
  - `follow_redirects=False`

  Auth headers are passed **per request**, not as client defaults. `aclose` closes the client if one was created.
- **`_get_json` steps.**
  1. Call `resolve_secret`. If it returns None, raise `MissingCredentialError(message=f"credential missing: env {api_key_env}")`.
  2. Send the GET. Catch `httpx.HTTPError` (transport) and raise `ProviderError(classify_transport_error(e), f"network: {type(e).__name__}") from None`. `from None` is mandatory so no request object with headers survives in `__cause__`/`__context__`.
  3. A non-2xx response raises `error_from_response(resp)`.
  4. On 2xx, `resp.json()`. A `ValueError` raises `MalformedResponseError` (`from None`).
  5. Log one DEBUG line: `provider=<id> GET <path> status=<n>`. Never log headers or the body.
- **`error_from_response` steps.**
  1. Try `resp.json()`. On failure the body is `None`.
  2. Get `(code, message, body_retry)` from `_parse_error_body`. A non-dict body gives `(None, "HTTP <status>", None)`.
  3. Set `retry_after_s = parse_retry_after(resp.headers.get("retry-after"), clock())`; if that is None, use `body_retry`.
  4. Set `request_id` to the first of `REQUEST_ID_HEADERS` present.
  5. Classify. Precedence: `ERROR_CODE_MAP[code.lower()]` (if the code is present and mapped), then `STATUS_OVERRIDES[status]`, then `classify_status(status)`.
  6. Redact the message: run it through `redactor.redact()` (if one is given), then also `.replace(api_key, REDACTED)` using the currently resolved key, then truncate to 300 chars.
- **`classify_error(error)`.**
  - `ProviderError` returns `error.error_class`.
  - `httpx.HTTPStatusError` returns `error_from_response(error.response).error_class`.
  - `httpx.HTTPError` or `TimeoutError` returns `classify_transport_error`.
  - Everything else returns `UNKNOWN`.
  - Any exception inside classification itself returns `UNKNOWN`.
- **`health_check()`.** Calls `_get_json(HEALTH_PATH, HEALTH_PARAMS)`.
  - Status mapping:
    - 2xx with a dict body: HEALTHY, detail `"ok"`.
    - `MissingCredentialError`: DOWN, `"credential missing: env <NAME>"`.
    - `ProviderError` with AUTH_FAILED: DOWN, `"HTTP <status>"`.
    - PROVIDER_UNAVAILABLE, TRANSIENT_NETWORK or TIMEOUT_UNKNOWN_OUTCOME: DOWN.
    - RATE_LIMITED, QUOTA_EXHAUSTED, `MalformedResponseError` or a non-dict body: DEGRADED.
    - Any other `ProviderError`: DEGRADED.
    - Any other `Exception`: DOWN, detail `f"error: {type(e).__name__}"`.
  - `checked_at=clock()`.
  - Details never contain bodies or keys.

### 4.4 Adapters

Each one subclasses `HttpProviderAdapter`, and its constructor defaults `provider_id` to the name below (signature `(config, *, provider_id=<name>, transport=None, environ=None, clock=utcnow, redactor=None)`). All four must satisfy `isinstance(a, ProviderAdapter)`.
- **Shape errors.** In `list_models`, a wrong top-level shape raises `MalformedResponseError`. Individual items that are not dicts, or lack a str `id`/`name`, are skipped.
- **Numbers.** Numeric fields are accepted only as an `int > 0` that is not a `bool`; anything else becomes `None`.
- **Metadata.** Every discovered capability is passed through `apply_metadata(cap, config.models.get(model_id))`.
- **Allowlist.** `list_models` returns **all** discovered models, both allowlisted and not. The allowlist is applied downstream (§7).

| Adapter (file) | provider_id | HEALTH_PATH | list_models | get_quota | Error specifics |
|---|---|---|---|---|---|
| `OpenRouterAdapter` (`openrouter.py`) | `openrouter` | `key` | `GET models` (A4) | `GET key`. If `limit` is a number: one record `(model=None, window="total", unit=USD, confidence=EXACT, limit, used=usage, remaining=limit_remaining, source="openrouter:key")`. If `limit` is null: one record with confidence UNKNOWN, `limit=None, remaining=None, used=usage`. If `data` is not a dict: `MalformedResponseError`. | `STATUS_OVERRIDES = {402: QUOTA_EXHAUSTED, 403: POLICY_REJECTED}`; `ERROR_CODE_MAP = {}`. OpenRouter codes are numeric, so status drives classification. |
| `GeminiAdapter` (`gemini.py`) | `gemini` | `models` with `HEALTH_PARAMS={"pageSize": "1"}` | paginated per A5 (`GEMINI_MAX_PAGES = 10`; log WARNING if truncated) | returns `[]` and sends no request (no authoritative source, A5) | `_parse_error_body` per A6. If any details item has reason `API_KEY_INVALID`, return code `"API_KEY_INVALID"`; otherwise the code is `status`. `ERROR_CODE_MAP = {"api_key_invalid": AUTH_FAILED, "resource_exhausted": RATE_LIMITED, "unauthenticated": AUTH_FAILED, "permission_denied": AUTH_FAILED, "not_found": MODEL_UNAVAILABLE, "invalid_argument": INVALID_REQUEST, "unavailable": PROVIDER_UNAVAILABLE, "internal": PROVIDER_UNAVAILABLE, "deadline_exceeded": TIMEOUT_UNKNOWN_OUTCOME}`. |
| `CerebrasAdapter` (`cerebras.py`) | `cerebras` | `models` | `GET models`. Items are `{"id"}` only, so capability flags default to False/None before config metadata is applied. | `GET models`, then `parse_rate_limit_headers(resp.headers, CEREBRAS_HEADER_DIMENSIONS, model=None)` (A8/A9). Absent headers give `[]`. | `ERROR_CODE_MAP = OPENAI_COMPAT_CODE_MAP` |
| `GroqAdapter` (`groq.py`) | `groq` | `models` | `GET models`. Read `context_window` and `max_completion_tokens` when present. Skip items with `"active": false`. | same as Cerebras, with `GROQ_HEADER_DIMENSIONS` (A7) | `ERROR_CODE_MAP = OPENAI_COMPAT_CODE_MAP` |

`CEREBRAS_HEADER_DIMENSIONS = {"requests-day": REQUESTS_PER_DAY, "tokens-minute": TOKENS_PER_MINUTE}`.
`GROQ_HEADER_DIMENSIONS = {"requests": REQUESTS_PER_DAY, "tokens": TOKENS_PER_MINUTE}`.

### 4.5 `app/providers/registry.py`
```python
ADAPTER_TYPES: Mapping[str, type[HttpProviderAdapter]] = {"openrouter": ..., "gemini": ..., "cerebras": ..., "groq": ...}
def build_adapters(config: AppConfig, *, transport=None, environ=None, clock=utcnow,
                   redactor: SecretRedactor | None = None) -> dict[str, HttpProviderAdapter]
    # one adapter per ENABLED config.providers key that is in ADAPTER_TYPES, in config order;
    # unknown provider keys: logger.warning("no adapter for provider %s") and skip
```

## 5. Cooldowns (`app/quota/cooldown.py`)

```python
class CooldownManager:
    def __init__(self, config: CooldownConfig, *, clock: Callable[[], datetime] = utcnow) -> None
    def record_failure(self, provider: str, model: str | None, error_class: ErrorClass, *,
                       retry_after_s: float | None = None, now: datetime | None = None) -> datetime | None
    def record_success(self, provider: str, *, now: datetime | None = None) -> None
    def set_cooldown(self, provider: str, model: str | None, until: datetime) -> None
    def active(self, now: datetime | None = None) -> dict[tuple[str, str | None], datetime]
    def is_cooling(self, provider: str, model: str | None, now: datetime | None = None) -> bool
```
`now` defaults to `clock()` in every method.

Rules for `record_failure`. It returns the `until` it applied, or None if it applied none.
- **`retry_after_s`.** A None or non-finite value means "not given".
- **Duration.** `d = min(max(d, 0), max_cooldown_s)`. If `d == 0`, apply no cooldown and return None. `until = now + timedelta(seconds=d)`.
- **Scope by error class.**
  - `RATE_LIMITED` or `QUOTA_EXHAUSTED`: key `(provider, model)`, or `(provider, None)` if model is None. Duration is `retry_after_s` if given, else `rate_limit_default_s`.
  - `PROVIDER_UNAVAILABLE`: key `(provider, None)`. Duration is `retry_after_s` if given, else `provider_unavailable_s`.
  - `TRANSIENT_NETWORK` or `TIMEOUT_UNKNOWN_OUTCOME`:
    1. Append `now` to the provider's failure deque (`deque(maxlen=network_failure_threshold)`).
    2. Drop entries `<= now - network_failure_window_s`.
    3. If the count is `>= threshold`, apply key `(provider, None)` for `retry_after_s` if given, else `network_failure_cooldown_s`, then clear the deque.
    4. Otherwise return None.
  - Every other class returns None and changes nothing.
- **`set_cooldown` and every apply.** Keep the **later** of the existing and new `until`. The return value is the `until` stored after this rule.
- **Logging.** Log INFO `cooldown provider=.. model=.. until=<iso> reason=<class>`.

Other methods:
- `record_success` clears that provider's network-failure deque. It does not lift active cooldowns.
- `active` returns a copy containing only entries with `until > now`, and deletes expired entries. This is how expiry happens automatically.
- `is_cooling` is True if `(provider, None)` or `(provider, model)` is active.
- State is in-memory only; persistence across restarts is deferred.

## 6. Quota normalization, persistence, usage, estimation

### 6.1 `app/quota/normalize.py`
```python
WINDOW_SECONDS: Mapping[str, int] = {"minute": 60, "hour": 3600, "day": 86400}
DIMENSION_SPECS: Mapping[QuotaDimension, tuple[str, QuotaUnit]]   # rpm->("minute",REQUESTS) ... tpd->("day",TOKENS), SPEND_USD->("total",USD)
ESTIMATE_SOURCE_PREFIX = "estimate:"
CONFIG_SOURCE_PREFIX = "config:"
def dimension_of(record: QuotaRecord) -> QuotaDimension | None
def validate_record(record: QuotaRecord) -> None   # raises ValueError
def normalize(records: Iterable[QuotaRecord]) -> dict[tuple[str, str | None], dict[QuotaDimension, QuotaRecord | None]]
```
`validate_record` raises `ValueError` for any of these:
- `limit`, `used` or `remaining` is a bool, non-finite, or negative.
- `source` is empty.
- `observed_at` or `reset_at` is naive.
- Confidence is EXACT or ESTIMATED and `remaining is None`.
- Confidence is EXACT and the source starts with `ESTIMATE_SOURCE_PREFIX` or `CONFIG_SOURCE_PREFIX`. This guarantees an estimate can never be labelled EXACT.

`normalize` rules:
- Every key `(provider, model)` gets **all 7 dimensions**. Missing dimensions are `None`, never 0.
- Records with an unrecognised `(window, unit)` are dropped.
- If several records share a dimension, keep the one with the greatest `observed_at`. On a tie, the later one in the input wins.

### 6.2 `app/db/quota.py`
```python
class QuotaSnapshotRepository:
    def __init__(self, conn) -> None
    def insert(self, record: QuotaRecord) -> int                  # validate_record first; returns rowid
    def insert_many(self, records: Sequence[QuotaRecord]) -> list[int]   # validate all first, then one transaction; all-or-nothing
    def latest(self, now: datetime) -> list[QuotaRecord]
```
- Field mapping to the existing migration-1 table: `QuotaRecord.limit` maps to `limit_value` and `QuotaRecord.window` to `quota_window`. All other fields map to columns of the same name. **No schema change.**
- `latest` returns the newest row per `(provider, COALESCE(model,''), quota_window, unit)`, ordered by `observed_at`, then `id`. It excludes rows with `reset_at IS NOT NULL AND reset_at <= to_db(now)`. Results are ordered by provider, model (NULL first), quota_window, unit.
- NUMERIC values come back as int or float, unchanged.

### 6.3 `app/db/usage.py`
```python
class UsageStatus(StrEnum): SUCCESS = "success"; ERROR = "error"; UNKNOWN_OUTCOME = "unknown_outcome"

@dataclass(frozen=True, slots=True)
class UsageEvent:
    provider: str; model: str; cost_class: CostClass; status: UsageStatus; occurred_at: datetime
    task_id: str | None = None; request_id: str | None = None
    input_tokens: int | None = None; output_tokens: int | None = None; estimated_cost_usd: float | None = None

@dataclass(frozen=True, slots=True)
class UsageTotals:
    requests: int; input_tokens: int; output_tokens: int
    events_missing_tokens: int        # events where input_tokens IS NULL OR output_tokens IS NULL
    earliest: datetime | None

@dataclass(frozen=True, slots=True)
class PaidSpend:
    total_usd: float; missing_cost_events: int

class UsageRepository:
    def __init__(self, conn) -> None
    def record(self, event: UsageEvent) -> int
    def totals(self, provider: str, *, model: str | None = None, since: datetime, until: datetime) -> UsageTotals
    def paid_spend(self, *, since: datetime, until: datetime) -> PaidSpend
    def daily_totals(self, now: datetime) -> list[tuple[str, str, UsageTotals]]   # (provider, model, totals) since UTC midnight, ordered by provider, model
```
- **`record` validation.** Raise `ValueError` for any of these:
  - token counts that are not None and not a non-negative int, or that are bools;
  - a cost that is not None and not finite and `>= 0`;
  - `cost_class == PAID` with `estimated_cost_usd is None`, because unknown paid cost is refused at write time;
  - an empty provider or model;
  - a naive `occurred_at`.
- **`record` write.** Writes inside `transaction()`. A `task_id` that is not None must reference an existing task (FK); the `sqlite3.IntegrityError` propagates.
- **Windows.** Both `totals` and `paid_spend` use the inclusive range `since <= occurred_at <= until`.
- **`totals`.** `model=None` sums all models of the provider. `requests = COUNT(*)` (A13). Token sums treat NULL as 0.
- **`paid_spend`.** `total_usd = SUM(estimated_cost_usd)` over `cost_class='PAID'`, using `COALESCE(..., 0.0)`. `missing_cost_events` counts PAID rows with NULL cost.

### 6.4 `app/quota/estimator.py`
```python
ESTIMATE_SOURCE = "estimate:config-limit-minus-usage"
def estimate_quota(config: AppConfig, usage: UsageRepository, now: datetime, *,
                   exact: Sequence[QuotaRecord] = ()) -> list[QuotaRecord]
```
For each **enabled** provider (in config order) and each of its `limits` (in list order):
1. Get `(window, unit)` from `DIMENSION_SPECS`. Skip this limit if `exact` contains a record with confidence EXACT for the same `(provider, model, window, unit)` and `reset_at is None or reset_at > now`.
2. Compute `t = usage.totals(provider, model=lim.model, since=now - WINDOW_SECONDS[window], until=now)`.
3. For REQUESTS: `used = t.requests`. For TOKENS: if `t.events_missing_tokens > 0`, emit a record with confidence **UNKNOWN**, `limit=lim.limit`, `used=None` and `remaining=None`, because missing token counts mean the number would be invented. Otherwise `used = input + output`.
4. Set `remaining = max(lim.limit - used, 0)`. Emit `QuotaRecord(provider, lim.model, window, unit, ESTIMATED, observed_at=now, source=ESTIMATE_SOURCE, limit=lim.limit, used=used, remaining=remaining, reset_at=t.earliest + window_len if t.earliest else None)`.

The estimator never emits EXACT. Every emitted record passes `validate_record`.

## 7. Router state (`app/quota/state.py`, `app/db/provider_models.py`, `app/router/types.py`)

### 7.1 `app/router/types.py`
Add `NoEligibleReason.ROUTER_STATE_UNAVAILABLE = "ROUTER_STATE_UNAVAILABLE"`. Change nothing else.

### 7.2 `app/db/provider_models.py`
```python
class ProviderModelRepository:
    def __init__(self, conn) -> None
    def replace_discovered(self, config: AppConfig, provider: str, models: Sequence[ModelCapability], now: datetime) -> None
    def allowed_capabilities(self, config: AppConfig, provider: str) -> list[ModelCapability]
```
`replace_discovered` runs in one transaction:
1. Upsert each model (`ON CONFLICT(provider, model) DO UPDATE`). `routing_status` is ALLOWED if `is_allowlisted(config, provider, model)`, otherwise DISCOVERED_ONLY. Bools are stored as 0/1, `None` as NULL. Keep the existing `discovered_at` on conflict and set `updated_at=now`.
2. Run `DELETE FROM provider_models WHERE provider=? AND updated_at <> ?(now)`, so models the provider no longer lists are dropped.

`allowed_capabilities` returns stored rows of that provider that are currently allowlisted (re-checked against config, not the stored status). NULL flags become False. Rows are ordered by model.

### 7.3 `app/quota/state.py`
```python
class RouterStateError(RuntimeError): ...

@dataclass(frozen=True, slots=True)
class ProviderObservation:
    provider: str
    health: ProviderHealth
    models: tuple[ModelCapability, ...]
    models_ok: bool
    observed_at: datetime

async def refresh_provider(adapter: ProviderAdapter, conn, config: AppConfig, *, now: datetime) -> ProviderObservation
async def refresh_all(adapters: Mapping[str, ProviderAdapter], conn, config: AppConfig, *, now: datetime,
                      environ: Mapping[str, str] | None = None) -> dict[str, ProviderObservation]
def build_router_state(config: AppConfig, conn, cooldowns: CooldownManager,
                       observations: Mapping[str, ProviderObservation], *, now: datetime,
                       environ: Mapping[str, str] | None = None) -> RouterState
def route_with_state(request: RouteRequest, config: AppConfig, state_factory: Callable[[], RouterState], *,
                     attempts: Sequence[FailedAttempt] = ()) -> RouteDecision
```
**`refresh_provider`** runs these steps in sequence for one provider. No step lets an exception escape.
- **Health.**
  - Call `health = await adapter.health_check()`.
  - If it raises anyway, use `ProviderHealth(DOWN, detail=f"error: {type(e).__name__}")`.
- **Models.**
  - Call `await adapter.list_models()`.
  - On success, run `ProviderModelRepository.replace_discovered` and set `models_ok=True`.
  - On an exception, log WARNING `provider=<p> list_models failed: <await adapter.classify_error(e)>`. Set `models=()` and `models_ok=False`.
- **Quota.**
  - Call `await adapter.get_quota()`, then `QuotaSnapshotRepository.insert_many`.
  - On an exception, log a WARNING with the class name or error class only.
- **Logging.** Log messages contain type names or ErrorClass values only, never `str(e)` of non-`ProviderError` exceptions.

**`refresh_all`** refreshes only providers that are enabled, in `credentials_present(config, environ)`, and in `adapters`. It uses `asyncio.gather` and returns `{provider: observation}`.

**`build_router_state`** is synchronous. It wraps everything so that any `ValueError`, `sqlite3.Error` or `TypeError` becomes `RouterStateError(f"cannot build router state: {e}") from e`.
- `credentials_present=credentials_present(config, environ)`.
- **`capabilities`.**
  - For each provider in `config.providers`, take `obs.models` if an observation exists with `models_ok`. Otherwise take `ProviderModelRepository.allowed_capabilities(...)`, the last known capabilities from the DB.
  - Keep only pairs where `is_allowlisted(config, provider, model)`, keyed `(provider, model)`. DISCOVERED_ONLY models never enter RouterState.
- `health={p: obs.health.status for p, obs in observations.items()}`.
- **`quota`.**
  - Set `exact = [r for r in QuotaSnapshotRepository(conn).latest(now) if not r.source.startswith(ESTIMATE_SOURCE_PREFIX)]`.
  - The quota list is `exact + estimate_quota(config, UsageRepository(conn), now, exact=exact)`.
- `cooldowns=cooldowns.active(now)`.
- **Spend.**
  - Compute `day = UsageRepository.paid_spend(since=<UTC midnight of now>, until=now)` and `month = paid_spend(since=<UTC 1st of month 00:00>, until=now)`.
  - If either has `missing_cost_events > 0`, raise `RouterStateError("paid usage event with unknown cost; refusing to route")`.
  - Pass `total_usd` values **unclamped** to `RouterState(...)`. A negative or non-finite value then raises `ValueError` in `RouterState.__post_init__`, which is wrapped as `RouterStateError`; its message contains the field name `paid_spend_...`. Never clamp spend to 0, because that would hide spend.

**`route_with_state`** calls `state_factory()`.
- On `RouterStateError`: log ERROR `router state unavailable: <msg>` and return `NoEligibleProvider(NoEligibleReason.ROUTER_STATE_UNAVAILABLE, ())`.
- Otherwise:
  - If `attempts` is empty, return `route(request, config, state)`.
  - If not, return `next_after_failure(request, attempts, config, state)`.
- Other exceptions propagate.

## 8. Redaction (`app/redaction.py`) and wiring

```python
REDACTED = "[REDACTED]"
SENSITIVE_HEADER_NAMES = ("authorization", "proxy-authorization", "x-api-key", "x-goog-api-key", "api-key", "api_key", "apikey")
class SecretRedactor:
    def __init__(self, secrets: Iterable[str] = (), *, min_length: int = 6) -> None
    def add(self, *secrets: str) -> None          # ignores values shorter than min_length (after strip); stores a frozenset, swapped atomically
    def redact(self, text: str) -> str
    def __repr__(self) -> str                      # f"SecretRedactor(<{n} secrets>)" only
    @classmethod
    def from_config(cls, config: AppConfig, environ: Mapping[str, str] | None = None) -> SecretRedactor   # uses collect_secret_values
class RedactingFilter(logging.Filter):
    def __init__(self, redactor: SecretRedactor) -> None
    def filter(self, record: logging.LogRecord) -> bool   # always True
def install_redaction(redactor: SecretRedactor, *, logger: logging.Logger | None = None) -> RedactingFilter
```
`app/redaction.py` must not import `app.providers.*`, to avoid an import cycle. It may import `app.config.secrets`.

- **`redact` steps.**
  1. Replace every known secret with `REDACTED`, longest first.
  2. Header and key patterns are case-insensitive. A sensitive header name, optionally quoted, followed by `:` or `=` and optional quotes and whitespace, then an optional `Bearer `/`Basic ` scheme, then a value `[^\s'",;&}\]]+`, has its value replaced with `REDACTED`. The name and scheme are kept.
  3. A query param `([?&](?:key|api_key|apikey)=)[^&\s'"#]+` becomes `\1[REDACTED]`.

  Examples:
  - `Authorization: Bearer sk-abc` → `Authorization: Bearer [REDACTED]`
  - `{'x-goog-api-key': 'AIza123'}` → `{'x-goog-api-key': '[REDACTED]'}`
  - `https://h/v1?key=AIza123&x=1` → `https://h/v1?key=[REDACTED]&x=1`
- **`RedactingFilter.filter` steps.**
  1. Set `msg = record.getMessage()`. If that fails, use `str(record.msg)`.
  2. Set `record.msg = redact(msg)` and `record.args = ()`.
  3. If `record.exc_info`: set `record.exc_text = redact(logging.Formatter().formatException(record.exc_info))`, then `record.exc_info = None`.
  4. If there is no `exc_info` but `record.exc_text` is set, redact it.
  5. If `record.stack_info` is set, redact it.
- **`install_redaction`.** Filters on a logger do **not** apply to records propagated from child loggers, so the filter goes on **handlers**. Add it to every handler of `logger` (default: the root logger) that does not already have a `RedactingFilter`, and return the filter.
- **`app/cli.py`.**
  - Right after `logging.basicConfig(...)`, create `redactor = SecretRedactor()` and call `install_redaction(redactor)`.
  - After `load_config` succeeds, call `redactor.add(*collect_secret_values(config))`.
- **Adapters.** They receive the redactor through `build_adapters(..., redactor=...)`. They always also redact their own resolved key (§4.3), even without a redactor.

## 9. Small follow-ups from `.pipeline/review.md` (included because trivial)

1. **`app/db/connection.py` `transaction()`.**
   - In the `except BaseException` branch, wrap `conn.execute("ROLLBACK")` in `try/except sqlite3.Error`.
   - On failure, log WARNING `rollback failed: <type name>` and re-raise the **original** exception.
   - Add `logger = logging.getLogger(__name__)`.
2. **`app/runtime/daemon.py` `submit_task()`.** If `self._stopped` is set, log WARNING `controller stopped; submission rejected` and return False before `put_nowait`.

Deferred, not in this slice: all router, retention and daemon **test-strengthening** follow-ups from review.md, and the TASKS.md T052 deferral note.

## 10. Tests (all offline)

### 10.1 `tests/conftest.py` additions
- **No-network guard.** An autouse fixture: `monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", _blocked)`, where `_blocked` is an async function that raises `RuntimeError("real network call attempted")`.
- **`SECRET`.** `SECRET = "sk-test-SECRET-0123456789"`.
- **`secret_env()`.** Returns `{"OPENROUTER_API_KEY": SECRET + "-or", "GEMINI_API_KEY": SECRET + "-ge", "CEREBRAS_API_KEY": SECRET + "-ce", "GROQ_API_KEY": SECRET + "-gq"}`.
- **`json_response(status, body, headers=None) -> httpx.Response`.**
- **`provider_cfg(**kw) -> ProviderConfig`** helper.

Adapters are always used as `async with Adapter(cfg, transport=httpx.MockTransport(handler), environ=..., clock=lambda: NOW)`. Handlers record `request.headers` and `request.url` for assertions.

### 10.2 Required cases

**test_provider_http_common**
- Parametrized `parse_duration` and `parse_number` cases from §4.2.
- `parse_retry_after`:
  - `"30"` gives 30.0.
  - An HTTP-date 90 s after NOW gives 90.0.
  - A date in the past gives 0.0.
  - `"soon"` and None give None.
- `parse_rate_limit_headers`:
  - With Groq-style headers (mixed case), it produces EXACT RPD and TPM records, with correct `used` and `reset_at`.
  - Remaining missing gives UNKNOWN.
  - No headers gives `[]`.
- **`classify_status`**, parametrized over 400, 401, 403, 404, 408, 413, 418, 422, 429, 500, 502, 503, 504, 507 and 302. Each expected class follows §4.3.
- **`classify_transport_error`**, for each httpx exception listed plus `TimeoutError` and `ValueError`.
- **`classify_error`** gives UNKNOWN for `ValueError`. It never raises, even for an exception whose `__str__` raises.

**test_provider_openrouter / gemini / cerebras / groq.** Each file must cover:
1. `isinstance(adapter, ProviderAdapter)`.
2. **Auth header sent.**
   - OpenRouter, Cerebras and Groq send `Authorization == f"Bearer {key}"`.
   - Gemini sends `x-goog-api-key == key`, and `"key=" not in str(request.url)`.
3. **`list_models` success.** Fields are parsed per §4.4. Config `models` metadata overrides discovered values. Non-dict items and items without an id are skipped. Gemini specifics:
   - The `models/` prefix is stripped.
   - Pagination follows `nextPageToken` across 2 pages.
   - Models without `generateContent` are dropped.
   - Pagination stops at 10 pages.
4. **Unlisted models.** `list_models` returns an unlisted discovered model. `model_listing(...)` then marks it DISCOVERED_ONLY.
5. **`health_check`.**
   - 200 gives HEALTHY.
   - 401 gives DOWN.
   - 429 gives DEGRADED.
   - 503 gives DOWN.
   - A `ConnectError` raised from the handler gives DOWN.
   - A non-JSON 200 gives DEGRADED.
   - A missing env var gives DOWN with detail containing the env var name, and **no request is sent** (the handler call count is 0).
6. **`get_quota`.**
   - OpenRouter:
     - A numeric limit gives an EXACT USD record.
     - A null limit gives UNKNOWN with `remaining is None`.
     - `data` as a list gives `MalformedResponseError`.
   - Gemini returns `[]` and makes no request.
   - Groq and Cerebras:
     - Headers present give EXACT records with `model is None` and source `"<p>:response-headers"`.
     - Headers absent give `[]`, never a zero.
7. **Error classification**, through `classify_error` on the raised `ProviderError` and also on `httpx.HTTPStatusError(response=...)`. Each of the following is classified correctly:
   - 429 with `Retry-After: 12` gives RATE_LIMITED, with `retry_after_s == 12` and `retry_after_ms == 12000`.
   - 401 gives AUTH_FAILED.
   - 5xx follows the common table.
   - A malformed 2xx in `list_models` gives `MalformedResponseError`, and the class is UNKNOWN.
   - A `ReadTimeout` gives TIMEOUT_UNKNOWN_OUTCOME.
   - A `ConnectError` gives TRANSIENT_NETWORK.

   Provider-specific cases:
   - **OpenRouter:**
     - 402 gives QUOTA_EXHAUSTED.
     - 403 gives POLICY_REJECTED.
   - **Gemini:**
     - 429 `RESOURCE_EXHAUSTED` with a RetryInfo `"7s"` and no header gives RATE_LIMITED with `retry_after_s == 7`.
     - 400 with details reason `API_KEY_INVALID` gives AUTH_FAILED.
     - 404 `NOT_FOUND` gives MODEL_UNAVAILABLE.
   - **Groq and Cerebras:**
     - 400 code `context_length_exceeded` gives CONTEXT_TOO_LARGE.
     - 404 `model_not_found` gives MODEL_UNAVAILABLE.
     - 429 with a non-JSON body gives RATE_LIMITED, classified by status.
8. **Secret safety.** Use a 401 whose body echoes the key, for example `{"error":{"message":"Incorrect API key provided: <key>"}}`. Then:
   - The key is in none of: `str(err)`, `repr(err)`, `"".join(traceback.format_exception(err))`, `repr(adapter)`, and caplog text at DEBUG.
   - `err.__cause__ is None`.
   - The same holds for a `ConnectError` path whose message contains the key.

**test_cooldown**
- A 429 on `(gemini, g-3.8)` applies until NOW+60 s (the default). `is_cooling("gemini", "g-3.7")` is False.
- Retry-After 10 gives NOW+10. A Retry-After of 99999 is clamped to `max_cooldown_s`. A Retry-After of 0 gives no cooldown. NaN uses the default.
- PROVIDER_UNAVAILABLE is provider-wide. `active()` contains `("x", None)`.
- Network failures:
  - Two TRANSIENT_NETWORK failures give no cooldown; the third gives a provider-wide cooldown.
  - Failures spaced wider than the window never trigger.
  - `record_success` resets the count.
- AUTH_FAILED, INVALID_REQUEST and UNKNOWN give no cooldown.
- A shorter new cooldown does not shorten an existing one.
- **Expiry.** `active(now=until)` excludes the entry (the boundary is expired) and prunes it.
- **Router integration.** Pass `cooldowns=mgr.active(NOW)` into `full_state`. A g-3.8 cooldown routes to g-3.7 when openrouter has an EXHAUSTED quota record. After expiry, it routes to g-3.8 again.

**test_quota_normalize**
- `DIMENSION_SPECS` covers all 7 dimensions with unique `(window, unit)` pairs.
- `normalize` with only an RPD record gives the other 6 dimensions as `None`.
- The newest `observed_at` wins.
- An unknown window is dropped.
- `validate_record` rejects each of: a negative value, NaN, a bool, an empty source, naive datetimes, EXACT with remaining None, and EXACT with source `"estimate:x"` or `"config:x"`.

**test_quota_repository** (uses the `db` fixture)
- An insert followed by `latest` round-trips every field, including `model=None` and both int and float values.
- `latest` returns only the newest per key.
- A row with `reset_at == now` is excluded.
- `insert_many` with one invalid record inserts nothing.
- Data survives reopening the DB.

**test_usage**
- `record` and `totals`:
  - Requests are counted across statuses.
  - The model filter works, and `model=None` sums all models.
  - The inclusive window bounds hold.
  - `events_missing_tokens` is counted.
  - `earliest` is correct.
- Validation errors are raised for: a PAID event with None cost, a negative cost, inf cost, negative tokens, a bool token count, and a naive time.
- An unknown `task_id` raises `sqlite3.IntegrityError`.
- `paid_spend` ignores FREE events.
- `daily_totals` excludes yesterday's events.

**test_estimator**
- Config gemini `limits=[{dimension: requests_per_day, limit: 5}]` with 3 events in the last 24 h and 1 event 25 h ago gives one ESTIMATED record: `used=3`, `remaining=2`, `window="day"`, `source=ESTIMATE_SOURCE`, and `reset_at == earliest + 24h`.
- Usage over the limit gives `remaining == 0`, never negative.
- A model-scoped limit counts only that model.
- A TOKENS limit with an event missing tokens gives UNKNOWN with `remaining=None`.
- A matching unexpired EXACT record in `exact` suppresses the estimate. An expired one does not.
- A disabled provider gives no records.
- No output record is ever EXACT.

**test_router_state** (`db` fixture, real adapters over MockTransport)
- **End to end.**
  1. Gemini adapter `list_models` returns g-3.8, g-3.7 and g-pro.
  2. Call `refresh_all(...)`, then `CooldownManager.record_failure("gemini", "g-3.8", RATE_LIMITED)`, then `build_router_state`.
  3. `route` selects `gemini/g-3.7` when openrouter has an EXHAUSTED snapshot persisted.
  4. `g-pro` is in `provider_models` as DISCOVERED_ONLY but is **not** a key of `state.capabilities`.

  The test config gives other providers' models through observations or DB rows as needed.
- Health 503 on cerebras gives `state.health["cerebras"] == DOWN`.
- Discovery failure (500 on list_models) falls back to the last DB rows, and capabilities are still present.
- A Gemini `requests_per_day` limit of 2 with 2 usage events gives an ESTIMATED remaining-0 record in `state.quota`. Routing skips Gemini with QUOTA_EXHAUSTED.
- **Bad spend.**
  - A PAID usage row with cost `-1.0`, inserted with raw SQL, makes `build_router_state` raise `RouterStateError` with `"paid_spend"` in the message, not `ValueError`.
  - A PAID row with NULL cost, via raw SQL, also raises `RouterStateError`.
  - `route_with_state(..., state_factory=lambda: build_router_state(...))` returns `NoEligibleProvider(ROUTER_STATE_UNAVAILABLE, ())`.
- `route_with_state` with a non-empty `attempts` delegates to `next_after_failure`, giving the same result as calling it directly.
- Paid spend today is summed from PAID rows of today only. A PAID row from yesterday counts toward the month total but not the day total. Pick NOW and "yesterday" in the same UTC month.

**test_redaction**
- The `redact` examples from §8 hold.
- Secrets shorter than `min_length` are ignored.
- Overlapping secrets are redacted longest first.
- `repr(redactor)` does not contain the secret.
- Run `logger.error("auth %s", f"Bearer {SECRET}")`, then `logger.exception(...)` inside `except` of `RuntimeError(f"bad key {SECRET}")`, through a `StringIO` handler with `install_redaction` applied. The output contains `[REDACTED]` and never `SECRET`.
- The same holds with a child logger, which proves the filter is on the handler.
- `install_redaction` twice does not add a second filter.
- `SecretRedactor.from_config(make_config(), secret_env())` redacts all four keys.

Each test must remove any handler or filter it adds to a logger in `finally`.

**test_config (additions)**
- Valid `limits`, `models`, `base_url`, `timeout_s` and `cooldown` load.
- Rejected, each with `match=`:
  - a `spend_usd` limit;
  - a `limit` of 0, NaN or inf;
  - duplicate `(model, dimension)`;
  - an unknown dimension;
  - a `base_url` of `"ftp://x"`;
  - `timeout_s` 0;
  - `cooldown.rate_limit_default_s` greater than `max_cooldown_s`;
  - `network_failure_threshold` 0.
- `config.example.yaml` still loads.
- `collect_secret_values` returns resolved provider keys and the Discord token, and skips unset ones.

**test_daemon (addition):** after `stop()`, `submit_task(...)` is False.

**test_db_connection (new)**
- `transaction()` commits on success and rolls back on exception.
- With a stub connection whose `execute` raises `sqlite3.OperationalError` only for `"ROLLBACK"`, the **original** exception (for example `KeyError`) propagates, not the OperationalError.

## 11. Edge cases checklist
- Missing quota data is `None` or `[]`, never 0. Absent headers never create records. UNKNOWN records never filter in the router (slice-1 behavior).
- Estimates are always ESTIMATED, or UNKNOWN when token counts are missing. `validate_record` rejects EXACT with an `estimate:` or `config:` source.
- Source contains no limit numbers. Configured limits come only from `providers.<p>.limits`.
- **Unlisted models.** `list_models` returns them. `provider_models` stores them as DISCOVERED_ONLY. `build_router_state` excludes them from `capabilities`.
- **Retry-After.** It can be seconds or an HTTP-date, and it is clamped to `[0, max_cooldown_s]`. 0 means no cooldown. Non-finite values use the default.
- **`now == until` or `now == reset_at`** is expired, consistent with slice 1.
- **Keys.**
  - Never in URLs (Gemini uses a header).
  - Never in errors, because messages are redacted and `from None` is used.
  - Never in reprs, logs, DB rows or dataclasses.
  - A missing key sends no request.
- **Errors.** `classify_error` and `health_check` never raise. `build_router_state` raises only `RouterStateError`.
- **Spend.** It is never clamped. Negative, non-finite or unknown paid cost fails closed with `RouterStateError`, and then `ROUTER_STATE_UNAVAILABLE`.
- **Windows.** Tests close every sqlite connection (`db` fixture) and every adapter client (`async with`).
- **No network in tests.** The autouse guard blocks `AsyncHTTPTransport`.

## 12. Out of scope (do not implement)
- Discord bot.
- OpenCode executor.
- Inference calls.
- Approvals.
- Quota refresh scheduler (T044).
- Daemon wiring of adapters or routing (T054).
- Persisting estimates or fallback_attempts.
- Cooldown persistence across restarts.
- Calendar-aligned (Pacific) daily resets.
- `/usage` and `/models` rendering.
- `.env.example`.
- The review.md test-strengthening follow-ups listed in §9.
