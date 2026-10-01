# Spec: MVP vertical slice 1 (offline core)

Covers T001, T001A, config loading, T020, T021, T021A, T030 (interfaces + fake), T050/T051/T051A/T053 (pure router + fallback selection).
Source docs: `SPEC.md`, `ARCHITECTURE.md`, `TASKS.md` at repo root.

## OPEN QUESTIONS

None. Every place where the docs conflict or leave a gap is decided below under "Decisions". Implement those decisions as written.

## Decisions (where the docs conflict or are silent)

1. **Config shape.** SPEC §16 and ARCH §7 disagree. This spec uses one merged shape (see §3): `routing.provider_order` lists **policy ids** (e.g. `gemini-free`), and `routing.policies.<policy_id>` holds `provider`, `cost_class`, `enabled`, and `model_order`. Provider priority is the **index in `provider_order`**. There is no numeric `priority` field. The per-policy `requires_approval` flag from ARCH is dropped. The global `routing.paid_requires_approval` is the only approval switch. `providers.openrouter.free_model` is dropped because `openrouter/free` is simply the single entry in `policies.openrouter-free.model_order`.
2. **Mode names.** The YAML values are `free-only`, `free-first-no-paid` (default), and `free-first-paid-after-approval`. `free-first-paid-after-confirmation` (the SPEC §6.6 wording) is accepted as an input alias for the third value. The Python enum names are `FREE_ONLY`, `FREE_FIRST_NO_PAID`, and `FREE_FIRST_PAID_AFTER_APPROVAL`. In the router, `FREE_ONLY` and `FREE_FIRST_NO_PAID` behave identically: paid candidates are never selected.
3. **Budget.** `daily_paid_budget_usd` defaults to `0.0`. `monthly_paid_budget_usd` defaults to `None`, meaning there is no monthly cap and the daily cap still applies. A paid selection, or a paid approval prompt, is only possible when the remaining budget is `> 0` (decision 4 has the exact rule). Cost prediction is out of scope (T113).
4. **`max_fallback_attempts` (default 3)** is the maximum **total** number of attempts in one task's chain, counting the initial attempt. This matches the `fallback_attempts.attempt_no` numbering. When `len(attempts_so_far) >= max_fallback_attempts`, there is no further candidate.
5. **Router outcomes are returned values (frozen dataclasses), not raised exceptions.** They are named `Selected`, `NoEligibleProvider`, and `PaidApprovalRequired`.
6. **The router is pure.** It does not read env vars, the DB, the clock, or the network. The caller passes in a `RouterState` snapshot that includes `now`. The caller updates cooldown/quota state before calling `next_after_failure` (SPEC §6.4 rule 2).
7. **Missing data.**
   - Missing capability metadata for a provider/model means the candidate is ineligible (`MODEL_UNKNOWN`), per SPEC §6.2 rule 2.
   - Missing health info for a provider means it is treated as `HEALTHY`.
   - Quota records whose `reset_at <= now` are ignored as expired. `UNKNOWN` confidence never filters.
8. **`large_context`** means `context_window is not None and context_window >= routing.large_context_min_tokens` (default 128000).
9. **Model override from Discord** is out of scope. Only the `is_allowlisted()` helper is provided.
10. **SQL column renames** avoid SQL keywords: `quota_snapshots.limit` becomes `limit_value`, `window` becomes `quota_window`, and `usage_events.timestamp` becomes `occurred_at`.
11. **Timestamps** are stored as TEXT in ISO-8601 UTC with microseconds (`2026-09-30T21:00:00.000000+00:00`), always produced by `app.timeutil.to_db()`. This makes lexicographic comparison valid.
12. **Persistence uses stdlib `sqlite3`** with explicit transactions. There is no ORM. The SQLite connection is used on the event loop thread. The retention job runs in `asyncio.to_thread` on its **own** connection.
13. **The daemon's task worker** only persists submitted tasks as `QUEUED` in this slice. Routing and execution wiring is the next slice. Discord and OpenCode are Protocol stubs with Null implementations.

## 0. Environment / tooling

- Python 3.12+. Create the venv with `py -3.14 -m venv .venv`. Do not use the bare `python` on PATH, which is an unrelated 3.11 interpreter.
- Install: `.venv\Scripts\python -m pip install -e ".[dev]"`
- Test: `.venv\Scripts\python -m pytest`
- Lint: `.venv\Scripts\python -m ruff check .` and `.venv\Scripts\python -m ruff format --check .`

## 1. Files to create

```text
.gitignore
pyproject.toml
README.md
config.example.yaml
scripts/setup_venv.ps1
scripts/check.ps1
app/__init__.py
app/__main__.py
app/cli.py
app/timeutil.py
app/config/__init__.py
app/config/models.py
app/config/loader.py
app/config/secrets.py
app/db/__init__.py
app/db/connection.py
app/db/migrations.py
app/db/tasks.py
app/db/retention.py
app/orchestrator/__init__.py
app/orchestrator/state_machine.py
app/providers/__init__.py
app/providers/base.py
app/providers/fake.py
app/router/__init__.py
app/router/types.py
app/router/router.py
app/router/policy.py
app/runtime/__init__.py
app/runtime/interfaces.py
app/runtime/daemon.py
tests/conftest.py
tests/test_config.py
tests/test_state_machine.py
tests/test_db_migrations.py
tests/test_task_repository.py
tests/test_retention.py
tests/test_providers_fake.py
tests/test_router.py
tests/test_router_fallback.py
tests/test_router_policy.py
tests/test_daemon.py
tests/test_cli.py
```

The repo has no existing code, so there are no patterns to copy. Follow the conventions below in every file:

- Every module starts with `from __future__ import annotations`.
- Domain types are `@dataclass(frozen=True, slots=True)`. Config uses Pydantic v2 models with `ConfigDict(extra="forbid", frozen=True)`.
- Enums are `enum.StrEnum`.
- No `print` outside `app/cli.py`. Use `logging.getLogger(__name__)`.

### 1.1 `.gitignore`

```
.venv/
__pycache__/
*.py[cod]
*.db
*.db-wal
*.db-shm
.env
data/
.pytest_cache/
.ruff_cache/
*.egg-info/
build/
dist/
```

### 1.2 `pyproject.toml`

- `[build-system]`: setuptools>=69, wheel. Package discovery uses `[tool.setuptools.packages.find] include = ["app*"]`.
- `[project]`: name `broke-ai-coder`, version `0.1.0`, `requires-python = ">=3.12"`, dependencies `pydantic>=2.12,<3` and `pyyaml>=6.0`.
- `[project.optional-dependencies] dev = ["pytest>=8.3", "pytest-asyncio>=0.24", "ruff>=0.6"]`
- `[project.scripts] agent-controller = "app.cli:main"`
- `[tool.pytest.ini_options]`: `testpaths = ["tests"]`, `asyncio_mode = "auto"`, `asyncio_default_fixture_loop_scope = "function"`.
- `[tool.ruff]`: `line-length = 100`, `target-version = "py312"`. Set `[tool.ruff.lint] select = ["E","F","I","B","UP"]`.

### 1.3 `scripts/setup_venv.ps1` / `scripts/check.ps1`

- `setup_venv.ps1` runs `py -3.14 -m venv .venv`. If that fails, it falls back to `py -3.12 -m venv .venv`. It then runs `.venv\Scripts\python -m pip install -e ".[dev]"`.
- `check.ps1` runs ruff check, ruff format --check, and pytest using `.venv\Scripts\python`. It exits non-zero on the first failure.

### 1.4 `README.md` (stub)

Include:

- A one-paragraph purpose statement.
- The venv setup commands from §0.
- `copy config.example.yaml config.yaml`.
- The run commands `.\.venv\Scripts\python -m app --check`, `--once`, and no flag.
- The test and lint commands.
- A note that API keys are read only from the env vars named in config.
- A note that Discord/OpenCode/provider adapters are not implemented yet.

### 1.5 `config.example.yaml`

It must validate with `load_config`; a test checks this. Content:

```yaml
routing:
  mode: free-first-no-paid
  paid_requires_approval: true
  daily_paid_budget_usd: 0
  monthly_paid_budget_usd: null
  max_fallback_attempts: 3
  large_context_min_tokens: 128000
  provider_order: [openrouter-free, gemini-free, cerebras-free, groq-free, openrouter-paid]
  policies:
    openrouter-free: {provider: openrouter, cost_class: FREE, model_order: [openrouter/free]}
    gemini-free:
      provider: gemini
      cost_class: FREE
      model_order: ["<gemini-flash-3.8-api-id>", "<gemini-flash-3.7-api-id>"]
    cerebras-free:
      provider: cerebras
      cost_class: FREE
      model_order: ["<cerebras-model-a-api-id>", "<cerebras-model-b-api-id>"]
    groq-free:
      provider: groq
      cost_class: FREE
      model_order: ["<groq-model-x-api-id>", "<groq-model-y-api-id>"]
    openrouter-paid:
      provider: openrouter
      cost_class: PAID
      model_order: ["<configured-paid-model-id>"]
providers:
  openrouter: {enabled: true, api_key_env: OPENROUTER_API_KEY}
  gemini:     {enabled: true, api_key_env: GEMINI_API_KEY}
  cerebras:   {enabled: true, api_key_env: CEREBRAS_API_KEY}
  groq:       {enabled: true, api_key_env: GROQ_API_KEY}
discord:
  bot_token_env: DISCORD_BOT_TOKEN
  allowed_user_ids: []
  allowed_guild_ids: []
  allowed_channel_ids: []
opencode:
  server_url: http://127.0.0.1:4096
  working_directory: /workspace
database:
  path: data/agent-controller.db
  busy_timeout_ms: 5000
  retention_days: 60
  cleanup_interval_hours: 24
  cleanup_batch_size: 500
runtime:
  task_queue_maxsize: 100
  event_queue_maxsize: 1000
  shutdown_timeout_s: 10
```

Add YAML comments explaining three points:

- `model_order` is an ordered hard allowlist.
- Placeholder IDs must be replaced with real API IDs.
- Keys are never placed in this file, only env var names.

## 2. `app/timeutil.py`

```python
def utcnow() -> datetime                    # aware, UTC
def to_db(dt: datetime) -> str              # raises ValueError if dt is naive; dt.astimezone(UTC).isoformat(timespec="microseconds")
def from_db(value: str) -> datetime         # datetime.fromisoformat; result is aware UTC
def to_db_opt(dt: datetime | None) -> str | None
def from_db_opt(value: str | None) -> datetime | None
```

## 3. Config (`app/config/`)

### 3.1 `models.py`

`CostClass` is imported from `app.providers.base`.

```python
class RoutingMode(StrEnum):
    FREE_ONLY = "free-only"
    FREE_FIRST_NO_PAID = "free-first-no-paid"
    FREE_FIRST_PAID_AFTER_APPROVAL = "free-first-paid-after-approval"


# A field_validator(mode="before") on RoutingConfig.mode maps "free-first-paid-after-confirmation" -> FREE_FIRST_PAID_AFTER_APPROVAL.

ENV_NAME_PATTERN = r"^[A-Z_][A-Z0-9_]*$"


class PolicyConfig(BaseModel):
    provider: str  # non-empty after strip
    cost_class: CostClass
    enabled: bool = True
    model_order: list[
        str
    ]  # min_length=1; each entry stripped and non-empty; duplicates -> ValueError


class RoutingConfig(BaseModel):
    mode: RoutingMode = RoutingMode.FREE_FIRST_NO_PAID
    paid_requires_approval: bool = True
    daily_paid_budget_usd: float = Field(0.0, ge=0)
    monthly_paid_budget_usd: float | None = Field(None, ge=0)
    max_fallback_attempts: int = Field(3, ge=1)
    large_context_min_tokens: int = Field(128_000, ge=1)
    provider_order: list[str] = []
    policies: dict[str, PolicyConfig] = {}
    # model_validator(after):
    #  - provider_order has no duplicates
    #  - every provider_order id exists in policies (unknown id -> ValueError naming it)
    #  - a (provider, model) pair appears in at most one policy (else ValueError, because cost class would be ambiguous)
    #  - policies not listed in provider_order are allowed; they are never routed


class ProviderConfig(BaseModel):
    enabled: bool = True
    api_key_env: str  # must match ENV_NAME_PATTERN; this rejects pasted keys such as "sk-or-..."


class DiscordConfig(BaseModel):
    bot_token_env: str = "DISCORD_BOT_TOKEN"  # ENV_NAME_PATTERN
    allowed_user_ids: list[int] = []
    allowed_guild_ids: list[int] = []
    allowed_channel_ids: list[int] = []


class OpenCodeConfig(BaseModel):
    server_url: str = "http://127.0.0.1:4096"
    working_directory: str = "/workspace"


class DatabaseConfig(BaseModel):
    path: str = "data/agent-controller.db"
    busy_timeout_ms: int = Field(5000, ge=0)
    retention_days: int = Field(60, ge=1)
    cleanup_interval_hours: float = Field(24, gt=0, le=24)  # cleanup runs at least daily
    cleanup_batch_size: int = Field(500, ge=1)


class RuntimeConfig(BaseModel):
    task_queue_maxsize: int = Field(100, ge=1)
    event_queue_maxsize: int = Field(1000, ge=1)
    shutdown_timeout_s: float = Field(10, gt=0)


class AppConfig(BaseModel):
    routing: RoutingConfig = RoutingConfig()
    providers: dict[str, ProviderConfig] = {}
    discord: DiscordConfig = DiscordConfig()
    opencode: OpenCodeConfig = OpenCodeConfig()
    database: DatabaseConfig = DatabaseConfig()
    runtime: RuntimeConfig = RuntimeConfig()
    # model_validator(after): every routing.policies[*].provider must be a key of providers (else ValueError)
```

All models use `ConfigDict(extra="forbid", frozen=True)`. Use `Field(default_factory=...)` for mutable defaults.

### 3.2 `loader.py`

```python
class ConfigError(Exception): ...
def load_config(path: str | os.PathLike[str]) -> AppConfig
def parse_config(data: Mapping[str, Any] | None) -> AppConfig
```

`load_config` behavior:

- **Missing file:** raise `ConfigError("config file not found: <path>")`.
- **YAML parsing:** use `yaml.safe_load`.
- **Empty file:** treat as `{}`, which produces a valid all-default config.
- **Non-mapping top level:** raise `ConfigError`.
- **Validation failures:** wrap `yaml.YAMLError` and `pydantic.ValidationError` in `ConfigError` with a readable message.

### 3.3 `secrets.py`

```python
def resolve_secret(env_name: str, environ: Mapping[str, str] | None = None) -> str | None
    # environ defaults to os.environ; an empty or whitespace-only value returns None. Never log the value.
def credentials_present(config: AppConfig, environ: Mapping[str, str] | None = None) -> frozenset[str]
    # provider names whose api_key_env resolves to a non-None secret
```

Secret values must never be stored on any config object or dataclass. Only env var names are stored.

## 4. Provider types (`app/providers/base.py`)

```python
class CostClass(StrEnum):
    FREE = "FREE"
    PAID = "PAID"


class QuotaConfidence(StrEnum):
    EXACT, ESTIMATED, UNKNOWN, EXHAUSTED, COOLDOWN  # value == name


class QuotaUnit(StrEnum):
    REQUESTS, TOKENS, USD


class HealthStatus(StrEnum):
    HEALTHY, DEGRADED, DOWN


class ErrorClass(StrEnum):
    (
        RATE_LIMITED,
        QUOTA_EXHAUSTED,
        MODEL_UNAVAILABLE,
        CONTEXT_TOO_LARGE,
        CAPABILITY_UNSUPPORTED,
    )
    (
        TRANSIENT_NETWORK,
        PROVIDER_UNAVAILABLE,
        AUTH_FAILED,
    )
    TIMEOUT_UNKNOWN_OUTCOME, INVALID_REQUEST, POLICY_REJECTED, UNKNOWN


PAIR_FALLBACK_ERRORS: frozenset[
    ErrorClass
]  # RATE_LIMITED, QUOTA_EXHAUSTED, MODEL_UNAVAILABLE, CONTEXT_TOO_LARGE, CAPABILITY_UNSUPPORTED, TRANSIENT_NETWORK
PROVIDER_FALLBACK_ERRORS: frozenset[
    ErrorClass
]  # PROVIDER_UNAVAILABLE, AUTH_FAILED  (excludes every model of that provider)
STOP_ERRORS: frozenset[
    ErrorClass
]  # TIMEOUT_UNKNOWN_OUTCOME, INVALID_REQUEST, POLICY_REJECTED, UNKNOWN
# Together the three sets cover every ErrorClass member exactly once (add a test for this).


@dataclass(frozen=True, slots=True)
class ModelCapability:
    provider: str
    model: str
    supports_tool_calling: bool = False
    supports_structured_output: bool = False
    supports_vision: bool = False
    context_window: int | None = None
    max_output_tokens: int | None = None


@dataclass(frozen=True, slots=True)
class QuotaRecord:
    provider: str
    model: str | None  # None = applies to every model of the provider
    window: str  # e.g. "minute", "hour", "day"
    unit: QuotaUnit
    confidence: QuotaConfidence
    observed_at: datetime
    source: str
    limit: int | float | None = None
    used: int | float | None = None
    remaining: int | float | None = None  # unknown -> None, never 0 (T040)
    reset_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class ProviderHealth:
    provider: str
    status: HealthStatus
    checked_at: datetime
    detail: str | None = None


@runtime_checkable
class ProviderAdapter(Protocol):
    provider_id: str

    async def health_check(self) -> ProviderHealth: ...
    async def get_quota(self) -> list[QuotaRecord]: ...
    async def list_models(self) -> list[ModelCapability]: ...
    async def classify_error(self, error: Exception) -> ErrorClass: ...
```

### 4.1 `app/providers/fake.py`

```python
class FakeProviderError(Exception):
    def __init__(self, error_class: ErrorClass, message: str = "") ...
    error_class: ErrorClass

class FakeProviderAdapter:
    def __init__(self, provider_id: str, *, models: Sequence[ModelCapability] = (),
                 quota: Sequence[QuotaRecord] = (), health: ProviderHealth | None = None) -> None
    # health defaults to HEALTHY with checked_at=utcnow()
    def set_models(self, models: Sequence[ModelCapability]) -> None
    def set_quota(self, quota: Sequence[QuotaRecord]) -> None
    def set_health(self, health: ProviderHealth) -> None
    calls: list[str]   # appends the method name on every async call
    # async methods return copies (new lists).
    # classify_error returns e.error_class for FakeProviderError, else ErrorClass.UNKNOWN.
```

It must pass `isinstance(adapter, ProviderAdapter)`. It makes no network or I/O calls.

## 5. Router (`app/router/`)

### 5.1 `types.py`

```python
@dataclass(frozen=True, slots=True)
class RequiredCapabilities:
    tool_calling: bool = False
    structured_output: bool = False
    vision: bool = False
    large_context: bool = False


@dataclass(frozen=True, slots=True)
class RouteRequest:
    task_id: str
    project_id: str
    required_capabilities: RequiredCapabilities = RequiredCapabilities()
    estimated_input_tokens: int = 0  # negative -> ValueError in __post_init__
    estimated_output_tokens: int = 0  # negative -> ValueError
    session_id: str | None = None
    paid_approved: bool = False  # an explicit /approve for paid use exists for this task


@dataclass(frozen=True, slots=True)
class RouterState:
    now: datetime  # aware
    credentials_present: frozenset[str] = frozenset()  # provider names
    capabilities: Mapping[tuple[str, str], ModelCapability] = {}  # (provider, model) -> capability
    quota: Sequence[QuotaRecord] = ()
    health: Mapping[str, HealthStatus] = {}  # provider -> status; missing = HEALTHY
    cooldowns: Mapping[
        tuple[str, str | None], datetime
    ] = {}  # (provider, model|None) -> until; None = whole provider
    paid_spend_today_usd: float = 0.0
    paid_spend_month_usd: float = 0.0
    # use field(default_factory=...) for the mapping defaults


@dataclass(frozen=True, slots=True)
class Candidate:
    policy_id: str
    provider: str
    model: str
    cost_class: CostClass
    provider_rank: int  # index in routing.provider_order
    model_rank: int  # index in policy.model_order

    @property
    def sort_key(self) -> tuple[int, int]:
        return (self.provider_rank, self.model_rank)


class SkipReason(StrEnum):
    (
        EXCLUDED_AFTER_FAILURE,
        POLICY_DISABLED,
        PROVIDER_DISABLED,
        MISSING_CREDENTIAL,
    )
    (
        PAID_BLOCKED_BY_MODE,
        PROVIDER_DOWN,
        COOLDOWN,
        MODEL_UNKNOWN,
        CAPABILITY_MISMATCH,
    )
    CONTEXT_TOO_SMALL, QUOTA_EXHAUSTED, QUOTA_INSUFFICIENT


@dataclass(frozen=True, slots=True)
class Skipped:
    candidate: Candidate
    reason: SkipReason


class NoEligibleReason(StrEnum):
    (
        FREE_CAPACITY_EXHAUSTED,
        PAID_BUDGET_EXHAUSTED,
        MAX_FALLBACK_ATTEMPTS_REACHED,
        NON_FALLBACK_ERROR,
    )


@dataclass(frozen=True, slots=True)
class Selected:
    candidate: Candidate
    skipped: tuple[Skipped, ...]


@dataclass(frozen=True, slots=True)
class NoEligibleProvider:
    reason: NoEligibleReason
    skipped: tuple[Skipped, ...]


@dataclass(frozen=True, slots=True)
class PaidApprovalRequired:
    candidates: tuple[Candidate, ...]  # eligible paid candidates in configured order (non-empty)
    skipped: tuple[Skipped, ...]


RouteDecision = Selected | NoEligibleProvider | PaidApprovalRequired


@dataclass(frozen=True, slots=True)
class FailedAttempt:
    provider: str
    model: str
    error_class: ErrorClass
```

### 5.2 `router.py`

```python
def build_candidates(config: AppConfig) -> list[Candidate]
def route(request: RouteRequest, config: AppConfig, state: RouterState, *,
          excluded_pairs: frozenset[tuple[str, str]] = frozenset(),
          excluded_providers: frozenset[str] = frozenset()) -> RouteDecision
def next_after_failure(request: RouteRequest, attempts: Sequence[FailedAttempt],
                       config: AppConfig, state: RouterState) -> RouteDecision
```

**`build_candidates`** iterates `for provider_rank, policy_id in enumerate(routing.provider_order)` and then `for model_rank, model in enumerate(policy.model_order)`. It yields one Candidate for each pair, including disabled policies and providers so that they show up in `skipped`. The output is already in sort-key order. **Never sort by anything other than `sort_key`.** Models not listed in config never become candidates.

**`route`** evaluates each candidate in list order. The first failing check, in this exact order, gives that candidate's `SkipReason`:

1. `(provider, model) in excluded_pairs` or `provider in excluded_providers` gives `EXCLUDED_AFTER_FAILURE`.
2. `not policy.enabled` gives `POLICY_DISABLED`.
3. `not providers[provider].enabled` gives `PROVIDER_DISABLED`.
4. `provider not in state.credentials_present` gives `MISSING_CREDENTIAL`.
5. `cost_class == PAID` and `mode != FREE_FIRST_PAID_AFTER_APPROVAL` gives `PAID_BLOCKED_BY_MODE`.
6. `state.health.get(provider, HEALTHY) == DOWN` gives `PROVIDER_DOWN`.
7. An active cooldown gives `COOLDOWN`. A cooldown is active when `cooldowns[(provider, None)]` or `cooldowns[(provider, model)]` is `> state.now`.
8. `(provider, model) not in state.capabilities` gives `MODEL_UNKNOWN`.
9. A required capability flag that is unsupported gives `CAPABILITY_MISMATCH`. For `large_context`, see Decision 8.
10. Safety threshold failures give `CONTEXT_TOO_SMALL`:
    - `context_window` is known and `est_in + est_out > context_window`, or
    - `max_output_tokens` is known and `est_out > max_output_tokens`.
11. Quota checks use the records that are applicable (`r.provider == provider and r.model in (None, model)`) and not expired (`r.reset_at is None or r.reset_at > now`). Process them in sequence order. The first record that triggers decides the reason:
    - `EXHAUSTED` gives `QUOTA_EXHAUSTED`.
    - `COOLDOWN` gives `COOLDOWN`.
    - `EXACT` or `ESTIMATED` with `remaining is not None`:
      - `REQUESTS` and `remaining < 1` gives `QUOTA_EXHAUSTED`.
      - `TOKENS` and `remaining < est_in + est_out` gives `QUOTA_INSUFFICIENT`.
      - `USD` is ignored.
    - `UNKNOWN` never filters.

After evaluating all candidates:

- If any FREE candidate is eligible, return `Selected(first eligible FREE)`.
- Otherwise, if no PAID candidate is eligible, return `NoEligibleProvider(FREE_CAPACITY_EXHAUSTED)`.
- Otherwise (paid eligible, which implies the paid-after-approval mode), check the budget. If `daily_paid_budget_usd - paid_spend_today_usd <= 0`, or the monthly cap is set and `monthly - paid_spend_month_usd <= 0`, return `NoEligibleProvider(PAID_BUDGET_EXHAUSTED)`.
- Otherwise, if `request.paid_approved` or `not routing.paid_requires_approval`, return `Selected(first eligible PAID)`.
- Otherwise return `PaidApprovalRequired(all eligible PAID, in order)`.

`skipped` always lists every skipped candidate in candidate order. Eligible-but-not-chosen candidates are not listed.

**`next_after_failure`**:

1. If `attempts` is empty, raise `ValueError`.
2. If `attempts[-1].error_class in STOP_ERRORS`, return `NoEligibleProvider(NON_FALLBACK_ERROR, skipped=())`.
3. If `len(attempts) >= routing.max_fallback_attempts`, return `NoEligibleProvider(MAX_FALLBACK_ATTEMPTS_REACHED, skipped=())`.
4. Set `excluded_pairs = {(a.provider, a.model) for a in attempts}` and `excluded_providers = {a.provider for a in attempts if a.error_class in PROVIDER_FALLBACK_ERRORS}`. Return `route(..., excluded_pairs=..., excluded_providers=...)`.

The same allowlist and paid gate therefore apply to fallback. FREE never crosses silently to PAID because paid candidates still go through the budget and approval gate.

### 5.3 `policy.py`

```python
class ModelRoutingStatus(StrEnum): ALLOWED = "ALLOWED"; DISCOVERED_ONLY = "DISCOVERED_ONLY"

@dataclass(frozen=True, slots=True)
class ModelListing:
    provider: str
    model: str
    status: ModelRoutingStatus
    policy_id: str | None     # None for DISCOVERED_ONLY
    rank: int | None          # model_order index; None for DISCOVERED_ONLY

def is_allowlisted(config: AppConfig, provider: str, model: str) -> bool
    # True if any policy for this provider lists the model (regardless of enabled flags)
def model_listing(config: AppConfig, provider: str, discovered: Iterable[str]) -> list[ModelListing]
    # First: ALLOWED entries for every policy of `provider`, policies in provider_order order and then
    #        policies not in provider_order in dict order, models in model_order order (listed even if not discovered).
    # Then: discovered models not allowlisted, de-duplicated, sorted alphabetically, as DISCOVERED_ONLY.
```

`app/router/__init__.py` re-exports `route`, `next_after_failure`, `build_candidates`, and all types.

## 6. Task state machine (`app/orchestrator/state_machine.py`)

```python
class TaskStatus(StrEnum): QUEUED, ROUTING, RUNNING, WAITING_APPROVAL, SUCCEEDED, FAILED, CANCELLED
TERMINAL_STATUSES: frozenset[TaskStatus] = {SUCCEEDED, FAILED, CANCELLED}
ALLOWED_TRANSITIONS: Mapping[TaskStatus, frozenset[TaskStatus]] = {
    QUEUED:           {ROUTING, CANCELLED},
    ROUTING:          {RUNNING, WAITING_APPROVAL, FAILED, CANCELLED},
    RUNNING:          {ROUTING, WAITING_APPROVAL, SUCCEEDED, FAILED, CANCELLED},   # RUNNING->ROUTING = request-time fallback
    WAITING_APPROVAL: {RUNNING, ROUTING, FAILED, CANCELLED},
    SUCCEEDED: frozenset(), FAILED: frozenset(), CANCELLED: frozenset(),
}
class InvalidTransition(Exception):  # attributes: from_status, to_status
def can_transition(current: TaskStatus, target: TaskStatus) -> bool
def ensure_transition(current: TaskStatus, target: TaskStatus) -> None   # raises InvalidTransition
```

Same-state transitions (for example `RUNNING -> RUNNING`) are invalid. Wrap the mapping in `types.MappingProxyType`.

## 7. Database (`app/db/`)

### 7.1 `connection.py`

```python
def open_database(path: str | os.PathLike[str], *, busy_timeout_ms: int = 5000) -> sqlite3.Connection
@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]
    # "BEGIN IMMEDIATE"; COMMIT on success; ROLLBACK on exception and re-raise
def db_size_bytes(conn: sqlite3.Connection) -> int   # PRAGMA page_count * PRAGMA page_size
```

`open_database` behavior:

- Create the parent directories.
- Call `sqlite3.connect(str(path), timeout=busy_timeout_ms/1000, isolation_level=None)` (autocommit, explicit transactions).
- Set `row_factory = sqlite3.Row`.
- Run these PRAGMAs: `journal_mode=WAL` (assert the returned value is `"wal"`, otherwise raise `RuntimeError`), `busy_timeout=<ms>`, `foreign_keys=ON`, `synchronous=NORMAL`.
- Do **not** run migrations here.

### 7.2 `migrations.py`

```python
@dataclass(frozen=True, slots=True)
class Migration:
    version: int
    statements: tuple[str, ...]

MIGRATIONS: tuple[Migration, ...]       # versions 1..N strictly increasing; this slice ships version 1 only
LATEST_VERSION: int
class SchemaTooNewError(RuntimeError): ...
def current_version(conn) -> int         # 0 if the schema_version table does not exist
def migrate(conn) -> int                 # returns the version after migrating
```

`migrate` behavior:

- `CREATE TABLE IF NOT EXISTS schema_version(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)`.
- If the current version is greater than `LATEST_VERSION`, raise `SchemaTooNewError`.
- Apply each pending migration in its own `transaction()`, executing each statement with `conn.execute` and then inserting its `schema_version` row.
- **Never use `executescript`.** It issues an implicit COMMIT and breaks atomicity.
- Running it twice is a no-op.

Migration 1 statements are the exact table set below. Use `TEXT` for ids and timestamps.

```sql
CREATE TABLE projects (id TEXT PRIMARY KEY, name TEXT NOT NULL UNIQUE, working_directory TEXT NOT NULL,
  git_remote TEXT, policy_id TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)

CREATE TABLE sessions (id TEXT PRIMARY KEY, opencode_session_id TEXT, project_id TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('OPEN','CLOSED')),
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL, closed_at TEXT)

CREATE TABLE tasks (id TEXT PRIMARY KEY, project_id TEXT NOT NULL,
  session_id TEXT REFERENCES sessions(id) ON DELETE SET NULL,
  discord_guild_id TEXT, discord_channel_id TEXT, discord_user_id TEXT NOT NULL, prompt TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('QUEUED','ROUTING','RUNNING','WAITING_APPROVAL','SUCCEEDED','FAILED','CANCELLED')),
  selected_provider TEXT, selected_model TEXT,
  cost_class TEXT CHECK (cost_class IS NULL OR cost_class IN ('FREE','PAID')),
  created_at TEXT NOT NULL, started_at TEXT, finished_at TEXT, last_event_at TEXT NOT NULL,
  exit_code INTEGER, error_class TEXT)
CREATE INDEX idx_tasks_status ON tasks(status)
CREATE INDEX idx_tasks_finished_at ON tasks(finished_at)
CREATE INDEX idx_tasks_session_id ON tasks(session_id)

CREATE TABLE provider_credentials_metadata (provider TEXT PRIMARY KEY, api_key_env TEXT NOT NULL,
  present INTEGER NOT NULL CHECK (present IN (0,1)), last_checked_at TEXT, last_auth_failure_at TEXT)

CREATE TABLE provider_models (provider TEXT NOT NULL, model TEXT NOT NULL,
  routing_status TEXT NOT NULL CHECK (routing_status IN ('ALLOWED','DISCOVERED_ONLY')),
  supports_tool_calling INTEGER, supports_structured_output INTEGER, supports_vision INTEGER,
  context_window INTEGER, max_output_tokens INTEGER,
  discovered_at TEXT NOT NULL, updated_at TEXT NOT NULL, PRIMARY KEY (provider, model))

CREATE TABLE provider_model_policy (id INTEGER PRIMARY KEY AUTOINCREMENT, provider TEXT NOT NULL, model TEXT NOT NULL,
  priority INTEGER NOT NULL, enabled INTEGER NOT NULL CHECK (enabled IN (0,1)),
  cost_class TEXT NOT NULL CHECK (cost_class IN ('FREE','PAID')),
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL, UNIQUE (provider, model))

CREATE TABLE quota_snapshots (id INTEGER PRIMARY KEY AUTOINCREMENT, provider TEXT NOT NULL, model TEXT,
  quota_window TEXT NOT NULL, limit_value NUMERIC, used NUMERIC, remaining NUMERIC,
  unit TEXT NOT NULL CHECK (unit IN ('REQUESTS','TOKENS','USD')),
  confidence TEXT NOT NULL CHECK (confidence IN ('EXACT','ESTIMATED','UNKNOWN','EXHAUSTED','COOLDOWN')),
  reset_at TEXT, observed_at TEXT NOT NULL, source TEXT NOT NULL)
CREATE INDEX idx_quota_snapshots_observed_at ON quota_snapshots(observed_at)

CREATE TABLE provider_events (id INTEGER PRIMARY KEY AUTOINCREMENT, provider TEXT NOT NULL, model TEXT,
  task_id TEXT REFERENCES tasks(id) ON DELETE SET NULL, event_type TEXT NOT NULL,
  http_status INTEGER, error_class TEXT, detail TEXT, created_at TEXT NOT NULL)
CREATE INDEX idx_provider_events_created_at ON provider_events(created_at)

CREATE TABLE approvals (id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
  session_id TEXT, discord_user_id TEXT, action_type TEXT NOT NULL, action_payload_hash TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('PENDING','APPROVED','DENIED','EXPIRED')),
  requested_at TEXT NOT NULL, expires_at TEXT NOT NULL, resolved_at TEXT, resolved_by TEXT)

CREATE TABLE usage_events (id INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id TEXT REFERENCES tasks(id) ON DELETE CASCADE, provider TEXT NOT NULL, model TEXT NOT NULL,
  request_id TEXT, input_tokens INTEGER, output_tokens INTEGER, estimated_cost_usd REAL,
  cost_class TEXT NOT NULL CHECK (cost_class IN ('FREE','PAID')), status TEXT NOT NULL, occurred_at TEXT NOT NULL)
CREATE INDEX idx_usage_events_occurred_at ON usage_events(occurred_at)

CREATE TABLE fallback_attempts (id INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE, session_id TEXT,
  attempt_no INTEGER NOT NULL, provider TEXT NOT NULL, model TEXT NOT NULL, status TEXT NOT NULL,
  error_class TEXT, http_status INTEGER, retry_after_ms INTEGER, started_at TEXT NOT NULL, finished_at TEXT,
  quota_snapshot_id INTEGER REFERENCES quota_snapshots(id) ON DELETE SET NULL, UNIQUE (task_id, attempt_no))
CREATE INDEX idx_fallback_attempts_started_at ON fallback_attempts(started_at)
```

No table has a column for raw API keys.

### 7.3 `tasks.py`

```python
@dataclass(frozen=True, slots=True)
class TaskRecord:
    id: str; project_id: str; session_id: str | None
    discord_guild_id: str | None; discord_channel_id: str | None; discord_user_id: str
    prompt: str; status: TaskStatus
    selected_provider: str | None; selected_model: str | None; cost_class: CostClass | None
    created_at: datetime; started_at: datetime | None; finished_at: datetime | None; last_event_at: datetime
    exit_code: int | None; error_class: str | None

class TaskNotFound(LookupError): ...
class ConcurrentTransition(RuntimeError): ...

class TaskRepository:
    def __init__(self, conn: sqlite3.Connection) -> None
    def create(self, *, project_id: str, prompt: str, discord_user_id: str,
               discord_guild_id: str | None = None, discord_channel_id: str | None = None,
               session_id: str | None = None, task_id: str | None = None,
               now: datetime | None = None) -> TaskRecord
        # id defaults to str(uuid.uuid4()); status QUEUED; created_at = last_event_at = now
    def get(self, task_id: str) -> TaskRecord | None
    def transition(self, task_id: str, target: TaskStatus, *, now: datetime | None = None,
                   error_class: str | None = None, exit_code: int | None = None) -> TaskRecord
    def list_unfinished(self) -> list[TaskRecord]   # non-terminal statuses, ordered by created_at, id
```

`transition` runs inside `transaction()` and does the following:

1. Read the current row. If it is missing, raise `TaskNotFound`.
2. Call `ensure_transition(current, target)`, which raises `InvalidTransition` and leaves the row unchanged.
3. Run `UPDATE ... WHERE id=? AND status=<current>`. If rowcount is 0, raise `ConcurrentTransition`.
4. Always set `last_event_at=now`.
5. When entering RUNNING, set `started_at` only if it is NULL.
6. When entering a terminal status, set `finished_at=now`.
7. If `error_class` or `exit_code` is given, set it. Otherwise leave it unchanged.
8. Return the fresh record.

`now` defaults to `utcnow()`. Naive datetimes raise `ValueError` via `to_db`. Discord IDs are stored as TEXT.

### 7.4 `retention.py`

```python
RETENTION_DAYS_DEFAULT = 60
@dataclass(frozen=True, slots=True)
class CleanupResult:
    cutoff: datetime
    deleted: dict[str, int]      # keys: usage_events, provider_events, fallback_attempts, quota_snapshots, tasks, sessions
    db_size_bytes: int
def run_retention_cleanup(conn, *, now: datetime | None = None, retention_days: int = 60,
                          batch_size: int = 500, checkpoint: bool = True) -> CleanupResult
def run_vacuum(conn) -> None     # executes VACUUM; never called by cleanup or the daemon in this slice
```

`run_retention_cleanup` rules:

- Compute `cutoff = (now or utcnow()) - timedelta(days=retention_days)` **once**. Every statement in the run uses this same `to_db(cutoff)` value.
- A **protected task** is any task whose status is not terminal, or whose `session_id` points to a session with `status='OPEN'`. Define one SQL fragment and reuse it:
  ```sql
  SELECT t.id FROM tasks t LEFT JOIN sessions s ON s.id = t.session_id
  WHERE t.status NOT IN ('SUCCEEDED','FAILED','CANCELLED') OR s.status = 'OPEN'
  ```
- Process the tables in this order. Each one uses a batched loop: `DELETE FROM <t> WHERE id IN (SELECT id FROM <t> WHERE <cond> LIMIT :batch)`. Each batch runs in its own `transaction()`. Repeat until a batch deletes fewer than `batch_size` rows. The totals are the sum of the batches.
  1. `usage_events`: `occurred_at < cutoff AND (task_id IS NULL OR task_id NOT IN (protected))`
  2. `provider_events`: `created_at < cutoff AND (task_id IS NULL OR task_id NOT IN (protected))`
  3. `fallback_attempts`: `started_at < cutoff AND task_id NOT IN (protected)`
  4. `quota_snapshots`: `observed_at < cutoff`. Referencing attempts get NULL through the FK.
  5. `tasks`: terminal status, `COALESCE(finished_at, created_at) < cutoff`, and the session is NULL or not OPEN. Approvals, usage events, and fallback attempts cascade. Provider events are set to NULL.
  6. `sessions`: `status='CLOSED' AND COALESCE(closed_at, updated_at) < cutoff AND NOT EXISTS (SELECT 1 FROM tasks WHERE tasks.session_id = sessions.id)`
- If `checkpoint` is true and the total deleted is greater than 0, run `PRAGMA wal_checkpoint(TRUNCATE)` outside any transaction.
- Never VACUUM here.
- `db_size_bytes` comes from `connection.db_size_bytes(conn)`.
- The function is idempotent: a second run with the same `now` deletes 0 rows from every table.
- Validate `retention_days >= 1` and `batch_size >= 1`, raising `ValueError` otherwise.

## 8. Runtime (`app/runtime/`)

### 8.1 `interfaces.py` (stubs only, no Discord or OpenCode code)

```python
class ChatFrontend(Protocol):            # future Discord bot
    async def start(self, controller: "AgentController") -> None: ...
    async def stop(self) -> None: ...
class NullFrontend:                      # logs "frontend disabled" on start; no-ops otherwise

@dataclass(frozen=True, slots=True)
class ExecutionResult:
    exit_code: int | None
    error_class: ErrorClass | None
class AgentExecutor(Protocol):           # future OpenCode executor; not wired in this slice
    async def run(self, *, task_id: str, provider: str, model: str, prompt: str,
                  session_id: str | None) -> ExecutionResult: ...
```

### 8.2 `daemon.py`

```python
@dataclass(frozen=True, slots=True)
class TaskSubmission:
    project_id: str
    prompt: str
    discord_user_id: str
    discord_guild_id: str | None = None
    discord_channel_id: str | None = None

@dataclass(frozen=True, slots=True)
class ControllerEvent:
    name: str
    at: datetime
    task_id: str | None = None
    fields: Mapping[str, str | int | float | bool | None] = field(default_factory=dict)

TaskHandler = Callable[["AgentController", TaskSubmission], Awaitable[None]]

class AgentController:
    def __init__(self, config: AppConfig, *, frontend: ChatFrontend | None = None,
                 task_handler: TaskHandler | None = None,
                 clock: Callable[[], datetime] = utcnow) -> None
    conn: sqlite3.Connection | None   # set by start()
    async def start(self) -> None
    async def stop(self) -> None
    async def run_forever(self) -> None
    async def run_once(self) -> None
    def request_stop(self) -> None
    def submit_task(self, submission: TaskSubmission) -> bool
    def emit(self, event: ControllerEvent) -> bool
    async def run_cleanup_now(self) -> CleanupResult
    def metrics(self) -> dict[str, int | str | None]
```

`AgentController` behavior:

- **`__init__`.** Creates the bounded queues `asyncio.Queue(maxsize=runtime.task_queue_maxsize)` for submissions and `asyncio.Queue(maxsize=runtime.event_queue_maxsize)` for events. It creates no unbounded buffers. `frontend` defaults to `NullFrontend()`.
- **`start()`.**
  - Opens the DB with `open_database(database.path, busy_timeout_ms=...)` and runs `migrate()`.
  - Creates exactly these asyncio tasks: the task worker, the event consumer, and the periodic cleanup loop. It does not use `run_once`; see below. It does not start subprocesses, threads that outlive a call, or helper daemons.
  - Calls `frontend.start(self)`.
  - Calling `start()` twice raises `RuntimeError`.
- **Task worker.**
  - Each submission is passed to `task_handler` (or the default handler). `task_done()` is always called.
  - Handler exceptions are logged with `logger.exception` and emit `ControllerEvent("task_handler_error")`. The worker keeps running.
  - The **default handler** calls `TaskRepository(conn).create(...)` from the submission and emits `ControllerEvent("task_queued", task_id=...)`.
- **Event consumer.** Logs each event at INFO as `event=<name> task_id=<id> k=v ...` and calls `task_done()`.
- **Cleanup loop.** Runs `run_cleanup_now()` immediately and then every `database.cleanup_interval_hours`. Exceptions are logged and do not crash the loop.
- **`run_cleanup_now`.** Runs `await asyncio.to_thread(...)`. The thread function opens its **own** connection with `open_database`, calls `run_retention_cleanup(now=clock(), retention_days, batch_size)`, and closes it in `finally`. The method stores `last_cleanup_at` and emits a `ControllerEvent("retention_cleanup", fields={table: count..., "db_size_bytes": n})`.
- **`submit_task`.** Calls `put_nowait`. It returns False if the queue is full and logs a warning. The submission is rejected, not buffered.
- **`emit`.** Calls `put_nowait`. If the queue is full, it increments `event_dropped_count` and returns False.
- **`stop()`.** Idempotent. It runs these steps in order:
  1. Set the stop event.
  2. Call `frontend.stop()`.
  3. Wait up to `shutdown_timeout_s` for `task_queue.join()`.
  4. Cancel the worker, consumer, and cleanup tasks and await them with `return_exceptions=True`. Log any remaining events with a best-effort drain.
  5. Close the DB connection.
- **`run_forever()`.** Calls `start()`, awaits the stop event, and then calls `stop()` in `finally`.
- **`run_once()`.** Calls `start()`, then calls `request_stop()` once the first cleanup has completed. Use an internal `asyncio.Event` set by the cleanup loop after its first run. It then calls `stop()`. It must finish in well under 5 s with an empty DB.
- **`metrics()`.** Returns these keys: `task_queue_depth`, `event_queue_depth`, `event_dropped_count`, `last_cleanup_at` (ISO string or None), and `sqlite_db_bytes` (None if the DB is not open).
- **Logging.** Never log config objects wholesale. There are no secrets in them anyway, but keep logs lean.

## 9. CLI (`app/cli.py`, `app/__main__.py`)

```python
def main(argv: Sequence[str] | None = None) -> int
```

- **Arguments:** `--config PATH` (default `config.yaml`). `--check` and `--once` are mutually exclusive.
- Configure logging with `logging.basicConfig(level=INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")`.
- **Config errors:** a `ConfigError` prints the message to stderr and returns `2`.
- **`--check`:**
  1. Load the config.
  2. Open the DB, run `migrate`, then close it.
  3. Print one line to stdout: `config OK; schema version <n>; database <path>`.
  4. Return 0.
- **`--once`:** runs `asyncio.run(controller.run_once())` and returns 0.
- **No flag:** runs `asyncio.run(controller.run_forever())`.
  - Try `loop.add_signal_handler(SIGINT/SIGTERM, controller.request_stop)` inside `run_forever`'s caller coroutine. Catch `NotImplementedError`, which happens on Windows, and rely on `KeyboardInterrupt`.
  - `KeyboardInterrupt` returns 0.
- **Other exceptions** are logged and return 1.
- **`__main__.py`:** `from app.cli import main` then `raise SystemExit(main())`.
- **`app/__init__.py`:** `__version__ = "0.1.0"`.

## 10. Tests (all offline, no network, no env secrets required)

`conftest.py` fixtures:

- `db` opens `tmp_path/"t.db"`, migrates, yields, and **closes** the connection. Windows cannot delete open SQLite or WAL files.
- `make_config(**overrides)` builds an `AppConfig` via `parse_config` with:
  - providers openrouter, gemini, cerebras, and groq;
  - policies `openrouter-free` [openrouter/free], `gemini-free` [g-3.8, g-3.7], `cerebras-free` [c-a, c-b], `groq-free` [q-x, q-y], `openrouter-paid` [or-paid-1];
  - provider order in that sequence.
- A `full_state(now)` helper builds a `RouterState` with:
  - all credentials present;
  - capabilities for every configured pair (tool calling true, context 200k, max_output 32k);
  - no quota, health, or cooldowns.

Required test cases (one or more test functions each):

- **test_config:**
  - `config.example.yaml` loads.
  - An empty file gives defaults (mode free-first-no-paid, budget 0, max attempts 3, retention 60).
  - The confirmation alias maps to the paid-after-approval mode.
  - An unknown provider_order id is rejected.
  - Duplicate provider_order entries are rejected.
  - A duplicate model within a policy is rejected.
  - The same (provider, model) in two policies is rejected.
  - A policy referencing an undefined provider is rejected.
  - An empty model_order is rejected.
  - `api_key_env: "sk-or-abc"` is rejected.
  - Extra unknown keys are rejected.
  - A negative budget is rejected.
  - `max_fallback_attempts: 0` is rejected.
  - A missing file raises ConfigError.
  - `credentials_present` with a fake environ returns only providers with non-empty values.
  - With the env var set to `"SECRET123"`, `"SECRET123"` appears nowhere in `repr(config)` or `config.model_dump_json()`.
- **test_state_machine:**
  - Every allowed transition passes.
  - Every pair not in the table raises InvalidTransition, including self-transitions.
  - Terminal states have no exits.
- **test_db_migrations:**
  - A fresh DB reaches LATEST_VERSION.
  - `PRAGMA journal_mode` returns `wal`.
  - `PRAGMA busy_timeout` equals the configured value.
  - `PRAGMA foreign_keys` is 1.
  - All 10 domain tables plus schema_version exist.
  - Running `migrate` twice is a no-op with one schema_version row per version.
  - A DB with a version above latest raises SchemaTooNewError.
- **test_task_repository:**
  - create gives QUEUED.
  - The valid path QUEUED→ROUTING→RUNNING→SUCCEEDED sets started_at and finished_at.
  - An invalid transition raises and leaves the row unchanged.
  - Transitioning a missing task raises TaskNotFound.
  - State survives closing and reopening the DB file (restart).
  - `list_unfinished` excludes terminal tasks.
  - The fallback path RUNNING→ROUTING→RUNNING keeps the original started_at.
- **test_retention** (fixed `now`; insert rows directly with SQL):
  - Old terminal tasks, their children, and old orphan telemetry are deleted.
  - Recent rows remain.
  - An old RUNNING task and its old fallback_attempts and usage_events remain.
  - An old SUCCEEDED task in an OPEN session remains.
  - An old CLOSED session with no tasks is deleted.
  - An old CLOSED session that still has a protected task remains.
  - A second run deletes 0 everywhere (idempotent).
  - `batch_size=1` still deletes everything eligible.
  - A row exactly at the cutoff is kept, because the comparison is strict `<`.
  - `db_size_bytes > 0`.
  - The cutoff equals now − 60 days.
- **test_providers_fake:**
  - isinstance ProviderAdapter holds.
  - Every async method returns the configured data.
  - classify_error maps FakeProviderError to its class and anything else to UNKNOWN.
  - The ErrorClass partition is complete and disjoint.
- **test_router** (T050/T051/T051A/T053/T081):
  - All free candidates are healthy, so openrouter/free is selected.
  - OpenRouter EXHAUSTED quota leads to gemini g-3.8 (T051 acceptance).
  - g-3.8 in cooldown leads to g-3.7, never a third Gemini model, even if `capabilities` contains an unlisted `g-pro` that is healthy.
  - Both Gemini models are ineligible, so cerebras c-a is selected.
  - Swapping g-3.8 and g-3.7 in config changes the selection with no code change.
  - Removing a model from config means it is never selected.
  - Capability mismatch skips the candidate (vision required, only q-y supports it, so q-y is selected).
  - large_context is enforced.
  - Estimated tokens above the context window give CONTEXT_TOO_SMALL.
  - Missing capability metadata gives MODEL_UNKNOWN.
  - Missing credential, disabled provider, and disabled policy are each skipped with the right reason.
  - Health DOWN is skipped and DEGRADED is allowed.
  - A provider-wide cooldown skips every model of that provider.
  - An expired cooldown or quota record (`until <= now`, `reset_at <= now`) does not filter.
  - An EXACT REQUESTS record with remaining 0 is skipped.
  - A TOKENS record with remaining below the estimate gives QUOTA_INSUFFICIENT.
  - An UNKNOWN record does not filter.
  - All free candidates are unavailable:
    - in the default mode, the result is NoEligibleProvider(FREE_CAPACITY_EXHAUSTED) and the paid candidate is skipped with PAID_BLOCKED_BY_MODE (T084 zero-spend);
    - FREE_ONLY behaves the same;
    - paid-after-approval with budget 0 gives PAID_BUDGET_EXHAUSTED;
    - with budget 5 and not approved, the result is PaidApprovalRequired containing or-paid-1;
    - with budget 5 and `paid_approved=True`, the paid candidate is Selected;
    - with `paid_requires_approval=False` and budget 5, the paid candidate is Selected;
    - with a monthly cap exhausted, the result is PAID_BUDGET_EXHAUSTED.
  - A free candidate is always chosen over paid even when the paid one is approved.
  - Determinism: same inputs give equal results.
  - `skipped` is in candidate order.
- **test_router_fallback:**
  - Attempts [openrouter/free RATE_LIMITED] lead to gemini g-3.8.
  - Then [+ g-3.8 RATE_LIMITED] lead to g-3.7 (T051A acceptance).
  - [+ g-3.7 QUOTA_EXHAUSTED] with max_fallback_attempts=5 leads to cerebras c-a.
  - The default max of 3 stops with MAX_FALLBACK_ATTEMPTS_REACHED after 3 attempts.
  - CONTEXT_TOO_LARGE on c-a leads to c-b.
  - AUTH_FAILED on openrouter/free excludes the whole openrouter provider, including the paid policy.
  - PROVIDER_UNAVAILABLE on gemini skips g-3.7.
  - TIMEOUT_UNKNOWN_OUTCOME, INVALID_REQUEST, and UNKNOWN each give NON_FALLBACK_ERROR.
  - Free-only mode with all free candidates failed never returns a paid candidate.
  - Fallback to paid in paid-after-approval mode returns PaidApprovalRequired, not Selected.
  - Empty attempts raise ValueError.
- **test_router_policy:**
  - `is_allowlisted` returns True for listed models and False for an unlisted one.
  - `model_listing("gemini", discovered=["g-pro","g-3.8","g-lite","g-pro"])` returns g-3.8 and g-3.7 as ALLOWED with ranks 0 and 1, then g-lite and g-pro as DISCOVERED_ONLY.
- **test_daemon** (config with a tmp DB path):
  - `run_once` completes, the DB file exists at the latest schema version, and `last_cleanup_at` is set.
  - submit_task is persisted as QUEUED via the default handler. Use start, submit, wait for queue join, check the DB, then stop.
  - With `task_queue_maxsize=1` and a handler blocked on an `asyncio.Event`, the second extra submission returns False.
  - With `event_queue_maxsize=1` and the consumer not draining, an emit that would overflow returns False and `event_dropped_count` increments. Test this on a controller that has not been started, so the queue is filled directly.
  - A handler raising does not kill the worker; a following submission is still processed.
  - stop is idempotent.
  - Calling start twice raises.
- **test_cli:**
  - `main(["--config", p, "--check"]) == 0` and the stdout contains "config OK".
  - A missing config returns 2.
  - An invalid config returns 2.
  - `subprocess.run([sys.executable, "-m", "app", "--config", p, "--once"], cwd=repo_root, timeout=60).returncode == 0`.

## 11. Edge cases checklist (the implementation must handle all of these)

- **Ordering:** any unlisted model never becomes a candidate. The router never reorders, and filters only remove.
- **Same provider in two policies:** the same provider under different policies (openrouter free and paid) shares provider-level cooldown, health, credential, and exclusion.
- **Naive datetimes** passed to any DB write raise `ValueError`.
- **`now` exactly equal** to a cooldown `until` or a quota `reset_at` means expired, not active.
- **Retention cutoff exactly equal** to a row's timestamp means kept.
- **Paid spend with budget 0:** paid can never be selected or prompted, regardless of `paid_approved`.
- **Windows:** close every sqlite connection in tests and at daemon stop. `add_signal_handler` raises `NotImplementedError` on Windows and must be caught.
- **No network:** no network imports or calls anywhere (no httpx, discord, or subprocess use in `app/`).

## 12. Out of scope (do not implement)

- Discord bot.
- OpenCode executor or subprocesses.
- Real provider HTTP adapters.
- Quota refresh/estimation jobs.
- Approvals flow/UI.
- Persisting router decisions or fallback attempts from the daemon.
- Discord model overrides.
- VACUUM scheduling.
- Resource benchmarks (T024).
- WSL setup.
- `.env.example`.
