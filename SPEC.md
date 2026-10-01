# SPEC.md — Discord-Controlled Free-First Coding Agent

> Version: 0.1
> Date: 2026-09-30
> Status: Draft / MVP specification
> Intended runtime: Windows PC + WSL2
>
> This project is designed around OpenCode as the coding-agent runtime, controlled remotely from an iPhone through Discord. The primary optimization target is **maximum useful coding work at $0**, with a hard safety boundary preventing accidental paid usage.

## 1. Product goal

Build a personal coding-agent control plane that lets the owner send coding tasks from the iPhone Discord app and have the agent execute them on a trusted home PC.

Core flow:

```text
iPhone Discord
    -> Discord Bot
    -> Agent Orchestrator
    -> Quota Manager / Model Router
    -> OpenCode
    -> selected LLM provider
    -> local repository / terminal / tests
    -> Discord progress + result
```

The system should automatically prefer free capacity, fall back between providers when a provider is unavailable or rate-limited, and require explicit approval before any paid model can be used. The Discord bot, orchestrator, quota manager, model router, provider adapters, LLM gateway, and SQLite ledger should run as a **single small daemon process**. OpenCode is a separate external runtime and is explicitly excluded from the daemon's RAM/CPU budget.

## 2. Primary objectives

1. Remote control from iPhone Discord.
2. Run real coding-agent work on the user's PC, not on Discord infrastructure.
3. Prefer free inference in a configurable order.
4. Route around provider-specific rate limits and outages.
5. Never spend money silently.
6. Preserve OpenCode's file-editing, shell, git, and test capabilities.
7. Support resumable sessions after Discord/SSH/network interruptions.
8. Keep API keys and source code on the trusted PC.
9. Make provider-specific quota logic replaceable because free-tier rules change frequently.
10. Produce enough telemetry to explain why a provider/model was selected.
11. Keep the controllable daemon lightweight enough to run continuously on a personal PC without introducing a separate database/cache service.

## 3. Non-goals for MVP

- Multi-user SaaS.
- Public Internet exposure of OpenCode.
- Automatic production deployment.
- Automatic `git push` without confirmation.
- Automatic paid spending.
- Training or hosting an LLM locally.
- Supporting every LLM provider on day one.

## 4. Initial providers

The initial provider set is:

| Priority | Provider | Intended role | Default paid? |
|---|---|---|---|
| 1 | OpenRouter `openrouter/free` | General free routing | No |
| 2 | Google Gemini API | Free Gemini coding capacity | No |
| 3 | Cerebras | High-throughput free fallback | No |
| 4 | Groq | Fast free fallback | No |
| 5 | Other free providers/models | Expansion slot | No |
| 99 | OpenRouter paid model | Last resort | **Blocked by default** |

Important: Qwen is treated as a **model family**, not a mandatory provider. A Qwen model may arrive through OpenRouter, Groq, Cerebras, or another provider. The router must reason about provider/model pairs, not assume a single Qwen API.

OpenRouter currently exposes a free-model router at `openrouter/free`; its current page says it selects free models dynamically and filters for capabilities such as tool calling. OpenRouter's free plan currently lists 25+ free models / 4 free providers and a 50 requests/day rate limit. These values are expected to change and therefore must not be hard-coded as permanent product rules. See the references list in `ARCHITECTURE.md` §16.

Gemini API free-tier rate limits are model- and project-dependent and are measured across dimensions such as RPM, TPM, and RPD. Google states that active limits can be viewed in AI Studio and that RPD resets at midnight Pacific. Exact limits are therefore configuration/runtime data, not constants in source code.

Groq and Cerebras expose provider-specific rate-limit information. Groq returns remaining/reset information in response headers; Cerebras documents free-tier TPM/TPH/TPD/RPM/RPH/RPD limits and says the authoritative limits for an organization are visible in its account.

## 5. User experience

### 5.1 Discord commands

MVP commands:

```text
/code <prompt> [project] [mode]
/status
/usage
/models
/sessions
/attach <session>
/cancel <session>
/approve <action_id>
/deny <action_id>
```

Example:

```text
/code Add pagination to the /transactions endpoint, update tests, and run the full test suite.
```

The bot should acknowledge quickly and then post progress updates.

Example progress:

```text
🤖 OpenCode started
Project: finance-app
Provider: openrouter
Model: openrouter/free
Quota confidence: estimated

🔎 Inspecting repository...
✏️ Editing 4 files...
🧪 Running tests...
```

Final response:

```text
✅ Task completed

Changed:
- api/transactions.py
- api/models.py
- tests/test_transactions.py

Tests: 52 passed
Git: working tree modified, no commit created
Provider: cerebras / gpt-oss-120b
Cost class: FREE
```

### 5.2 Destructive-operation approval

Any of these operations require approval unless explicitly disabled for a trusted local project:

- `git push`
- deleting many files
- deleting a repository
- modifying deployment configuration
- production deployment commands
- package installation that changes the environment
- switching from free to paid inference

The bot must present the exact command/action before requesting approval.

Example:

```text
⚠️ Approval required
Action: git push origin main
Reason: OpenCode requested this command.

[Approve] [Deny]
```

## 6. Model-routing requirements

### 6.1 Router input

For every new OpenCode task, the router receives:

```json
{
  "task_id": "uuid",
  "project_id": "finance-app",
  "required_capabilities": {
    "tool_calling": true,
    "structured_output": false,
    "vision": false,
    "large_context": true
  },
  "estimated_input_tokens": 20000,
  "estimated_output_tokens": 5000,
  "session_id": "uuid",
  "user_mode": "free-first"
}
```

### 6.2 Candidate filtering

A candidate is eligible only if:

1. The provider credential exists.
2. The model is currently known/usable.
3. Required capabilities are supported.
4. Provider is not in cooldown.
5. Quota state is not `EXHAUSTED`.
6. The candidate is allowed by the user's routing policy.
7. Estimated usage is below configured safety thresholds.

### 6.3 Selection order and per-provider model allowlists

Provider priority and model priority are **separate, declarative policies**.

Default provider order:

```text
FREE-FIRST
    1. OpenRouter free router
    2. Gemini
    3. Cerebras
    4. Groq
    5. Other explicitly configured free providers
    6. PAID is not selected automatically
```

For Gemini, Cerebras, Groq, and any other provider requiring model-level control, define an **ordered model allowlist**. The allowlist is a hard boundary:

```text
provider/model not in allowlist
    -> NEVER eligible
```

Example policy intent:

```text
Gemini:
    Flash 3.8
    -> Flash 3.7
    -> stop for Gemini

Cerebras:
    Model A
    -> Model B
    -> stop for Cerebras

Groq:
    Model X
    -> Model Y
    -> stop for Groq
```

The router must **not automatically discover and use a different model** merely because it is available. Model discovery may populate metadata and health information, but eligibility is controlled by the configured allowlist.

Within a provider, models are attempted strictly in configured order. Quota, health, and capability checks may skip an ineligible model, but must not promote a later model ahead of an eligible earlier model.

After all configured models for a provider are exhausted or unavailable, the router moves to the next provider in the provider-priority order.

For `openrouter/free`, the router treats `openrouter/free` as one candidate. OpenRouter may dynamically select an underlying free model; the control plane does not assume or promise which underlying model is used unless the provider supports and the administrator configures a narrower policy.

Operational signals such as quota confidence, provider health, capability compatibility, recent success rate, and latency may determine eligibility, but they **must not override explicit provider/model order**.

The router must not rank models by an overall quality score. It is an operational routing policy, not a model-quality leaderboard.

### 6.4 Request-time fallback

A preflight quota check cannot guarantee admission. If a request is sent and the provider then returns a quota/rate-limit/availability error, the controller must automatically try the next eligible **provider/model pair** under the current policy.

```text
openrouter/free
   ↓ HTTP 429 / quota_exceeded
record attempt + cooldown
   ↓
gemini-flash
   ↓ HTTP 429
record attempt + cooldown
   ↓
cerebras-model
   ↓
success
```

Rules:

1. Record every attempt, including failed attempts.
2. Update quota/cooldown state from the error before rerouting.
3. Exclude the failed provider/model from the current fallback chain.
4. Re-select based on capability + quota + policy.
5. Keep the same parent `task_id` and continue the OpenCode session when the documented `--session` + `--model` path is compatible. OpenCode currently documents both flags.
6. Enforce `max_fallback_attempts` (recommended default: 3).
7. Never silently cross from FREE to PAID. A paid candidate enters `WAITING_APPROVAL` unless policy explicitly allows it.

### 6.5 Model switching

Model switching is constrained by the configured per-provider model allowlist.

The fallback engine may switch to the **next configured model for the same provider** when the failure is model-specific, such as:

- model unavailable
- model-specific quota exhausted
- context window too small
- output limit too small
- required tool/structured-output capability unavailable

It may then switch to the next configured provider according to provider priority.

Example:

```text
Gemini Flash 3.8
   ↓ quota/model error
Gemini Flash 3.7
   ↓ quota/model error
Cerebras Model A
   ↓ quota/model error
Cerebras Model B
   ↓
Groq Model X
```

If `Gemini Flash 3.8` fails, the router must never jump to an unconfigured Gemini model such as a Pro, Lite, Preview, Experimental, or other family member.

Do not treat every 5xx/timeout as proof that the provider did not process the request. Unknown-outcome failures must be logged and handled conservatively rather than blindly replayed.

### 6.6 Paid fallback

Paid inference is blocked by default.

Three explicit modes are supported:

```text
free-only
free-first-no-paid
free-first-paid-after-confirmation
```

If paid fallback is enabled, the system must require one of:

- explicit `/approve` for the current task, or
- a project-level policy with a configured hard daily/monthly budget.

Default budget:

```text
USD 0.00
```

A provider may not spend above the configured budget even if OpenCode requests it.

## 7. Quota model

The system must distinguish these states:

```text
EXACT       -> provider gave authoritative remaining data
ESTIMATED   -> locally estimated from observed usage/rules
UNKNOWN     -> no reliable remaining figure available
EXHAUSTED   -> provider rejected requests for quota/rate-limit reasons
COOLDOWN    -> temporarily unavailable after repeated failures
```

Quota record example:

```json
{
  "provider": "groq",
  "model": "openai/gpt-oss-120b",
  "window": "day",
  "limit": 1000,
  "used": 243,
  "remaining": 757,
  "unit": "requests",
  "confidence": "exact",
  "reset_at": "2026-10-01T00:00:00-07:00",
  "observed_at": "2026-09-30T14:00:00-07:00"
}
```

For Gemini, if the API does not expose an authoritative remaining count, the adapter may maintain an estimate from local usage plus observed 429 errors. The UI must label the value as `estimated` rather than exact.

## 8. Provider adapter requirements

All providers implement a common interface:

```python
class ProviderAdapter(Protocol):
    provider_id: str

    async def health_check(self) -> ProviderHealth: ...
    async def get_quota(self) -> list[QuotaRecord]: ...
    async def list_models(self) -> list[ModelCapability]: ...
    async def classify_error(self, error: Exception) -> ErrorClass: ...
```

Provider-specific behavior must remain inside adapters.

Initial adapters:

```text
OpenRouterAdapter
GeminiAdapter
CerebrasAdapter
GroqAdapter
```

Future adapters should be addable without changing the router core.

## 9. OpenCode integration requirements

OpenCode remains the coding-agent runtime. The control plane does not reimplement file editing or tool execution.

Supported integration pattern:

```text
opencode serve
opencode run --attach <server> --model <provider/model> "<prompt>"
```

OpenCode currently supports headless server mode and non-interactive `opencode run`, including `--model`, `--session`, and `--attach`. It also supports provider/model configuration and custom OpenAI-compatible providers. The implementation should prefer documented OpenCode interfaces rather than relying on private internals.

The orchestrator owns:

- which model/provider is selected
- when a task may start
- whether paid mode is allowed
- Discord lifecycle
- quota accounting
- retries/fallback

OpenCode owns:

- repo inspection
- file edits
- shell/tool calls
- coding-agent reasoning
- tests
- git operations requested by the agent

## 10. Session management

Each Discord task maps to an OpenCode session.

Required persisted fields:

```text
task_id
session_id
project_id
discord_guild_id
discord_channel_id
discord_user_id
prompt
status
selected_provider
selected_model
created_at
started_at
finished_at
last_event_at
exit_code
error_class
```

Supported statuses:

```text
QUEUED
ROUTING
RUNNING
WAITING_APPROVAL
SUCCEEDED
FAILED
CANCELLED
```

A task must be resumable after a bot restart when the OpenCode session is still available.

## 11. Security requirements

### Secrets

API keys must never be stored in Discord messages or Git.

Preferred storage order:

1. OS/WSL environment variables or local secret store.
2. OpenCode credential storage where appropriate.
3. Encrypted application config only if necessary.

### Network

Recommended deployment:

```text
iPhone
  -> Discord
  -> bot
  -> home PC
```

Do not expose OpenCode port 4096 directly to the public Internet.

Bind OpenCode to localhost when possible. If remote access to the bot/orchestrator is needed, use a private network such as Tailscale and protect OpenCode with its documented HTTP Basic auth support.

### Discord authorization

Only allow configured Discord user IDs and optionally configured guild/channel IDs.

Unknown users must receive no agent access.

### Command safety

All shell/tool activity is executed with the same OS permissions as the OpenCode process. Therefore the machine account itself must have least privilege.

## 12. Persistence

MVP database: SQLite, embedded in the daemon. No separate Redis/PostgreSQL/database service is required.

### 12.1 Retention

The SQLite ledger keeps **60 days of historical telemetry/usage data**. A scheduled cleanup job runs at least daily and removes historical records older than 60 days.

Retention applies to high-volume/history tables such as:

```text
usage_events
provider_events
fallback_attempts
quota_snapshots
completed/failed task history
```

Active or resumable work must not be deleted solely because it is older than 60 days. Operational records needed to preserve an active OpenCode session may remain until the session is closed and then become eligible for retention cleanup.

After deletion, SQLite maintenance should reclaim space periodically using a controlled maintenance operation such as `VACUUM` during a low-activity window, not after every delete.

### 12.2 Resource budget — controlled components only

The following is the engineering target for the **Agent Controller daemon only**:

```text
Idle RAM target:       < 250 MB
Normal working RAM:    < 512 MB
Sustained idle CPU:    < 2%
Normal controller CPU: < 10%

SQLite: embedded/no separate process
Redis/Postgres: not used in MVP
Additional worker daemons: not used in MVP
```

These are design budgets, not guarantees. The daemon must avoid unbounded in-memory logs, queues, cached model catalogs, or Discord message buffers. Large OpenCode output must be streamed/truncated rather than accumulated indefinitely.

**Explicitly out of scope for this budget:** OpenCode itself, the user's project build/test processes launched by OpenCode, Docker containers launched by the project, the WSL2 VM baseline, Windows, and remote provider inference. Those workloads may consume additional CPU/RAM and are not counted as daemon resource usage.


Tables:

```text
projects
sessions
tasks
provider_credentials_metadata
provider_models
quota_snapshots
provider_events
approvals
usage_events
fallback_attempts
```

Do not store raw API keys in SQLite.

### fallback_attempts

```text
id
task_id
session_id
attempt_no
provider
model
status
error_class
http_status
retry_after_ms
started_at
finished_at
quota_snapshot_id
```

## 13. Single-daemon architecture

The MVP control plane is one long-running daemon process:

```text
agent-controller
  ├─ Discord bot
  ├─ task/session manager
  ├─ quota manager
  ├─ model router
  ├─ provider adapters
  ├─ LLM gateway/proxy (if enabled)
  └─ SQLite ledger
```

Components communicate through in-process async queues/calls where practical. Do not introduce Redis, RabbitMQ, Postgres, or a second API server just to connect these components.

## 14. Observability

Every task must log structured events:

```json
{
  "task_id": "uuid",
  "timestamp": "2026-09-30T14:20:00-07:00",
  "event": "provider_selected",
  "provider": "cerebras",
  "model": "gpt-oss-120b",
  "quota_confidence": "exact",
  "reason": "OpenRouter unavailable; Cerebras free capacity available"
}
```

Required counters:

- tasks started/completed/failed
- provider requests
- provider failures by class
- rate-limit events
- estimated tokens
- observed tokens when returned
- free vs paid tasks
- paid spend
- approval count
- provider fallback count

## 15. Reliability rules

Retry only errors that are safe to retry.

Retryable:

- transient network errors
- HTTP 429
- selected 5xx responses
- provider temporary unavailability

Non-retryable by default:

- invalid API key
- invalid request
- unsupported capability
- authentication failure
- policy/security rejection

For a retry/fallback:

1. preserve the same user task
2. record the failure reason
3. update provider health/quota
4. select the next eligible candidate
5. continue the task if the session can be safely continued

Do not blindly replay a tool action that may have side effects.

## 16. Configuration

Example configuration concept:

```yaml
routing:
  mode: free-first-no-paid
  paid_requires_approval: true
  daily_paid_budget_usd: 0
  provider_order:
    - openrouter-free
    - gemini-free
    - cerebras-free
    - groq-free
    - other-free
    - openrouter-paid

  # Hard ordered model allowlists.
  # Use the provider's exact API model IDs in real configuration.
  model_order:
    gemini:
      - <gemini-flash-3.8-api-id>
      - <gemini-flash-3.7-api-id>
    cerebras:
      - <cerebras-model-a-api-id>
      - <cerebras-model-b-api-id>
    groq:
      - <groq-model-x-api-id>
      - <groq-model-y-api-id>

providers:
  openrouter:
    enabled: true
    api_key_env: OPENROUTER_API_KEY
    free_model: openrouter/free

  gemini:
    enabled: true
    api_key_env: GEMINI_API_KEY

  cerebras:
    enabled: true
    api_key_env: CEREBRAS_API_KEY

  groq:
    enabled: true
    api_key_env: GROQ_API_KEY

discord:
  allowed_user_ids: []
  allowed_guild_ids: []
  allowed_channel_ids: []

opencode:
  server_url: http://127.0.0.1:4096
  working_directory: /workspace
```

Model-policy rules:

1. `model_order.<provider>` is an ordered hard allowlist.
2. A model absent from the allowlist is never eligible for routing.
3. Discovery may report unlisted models, but they are metadata-only (`DISCOVERED_ONLY`).
4. Reordering the list changes fallback order without code changes.
5. Removing a model prevents new tasks and fallback attempts from selecting it.
6. A configured model must still pass capability, quota, health, and cost-policy checks.
7. The same allowlist applies to initial routing and request-time fallback.
8. Arbitrary model overrides from Discord are rejected unless the model is explicitly allowlisted for that provider.

The exact API IDs for examples such as "Gemini Flash 3.8" and "Gemini Flash 3.7" must be supplied in deployment configuration; the core application must not infer or substitute alternate Gemini families.

## 17. Acceptance criteria for MVP

The MVP is complete when all are true:

1. From an iPhone Discord app, an authorized user can submit `/code`.
2. The task runs on the home PC through OpenCode.
3. The router selects a configured free candidate when one is eligible.
4. At least OpenRouter and Gemini are supported.
5. At least one of Cerebras or Groq is supported as a fallback.
6. The system can detect and handle a provider 429 without crashing the task controller.
7. A request-time 429/quota/availability failure causes the next eligible provider/model to be attempted when available.
8. A model-specific failure can switch to another compatible model without losing the parent task/session identity.
9. Gemini/Cerebras/Groq selection is restricted to the configured per-provider ordered model allowlists; no unlisted model can ever be selected.
10. `/status` shows current task/provider/session state.
11. `/usage` shows provider quota state with an explicit confidence label.
12. Paid inference cannot happen unless the configured paid policy permits it.
13. `git push` requires approval by default.
14. API keys never appear in Discord responses or logs.
15. Restarting the Discord bot does not corrupt persistent task records.
16. OpenCode remains usable directly from the PC.
17. A provider can be disabled without code changes to the core router.
18. Historical SQLite telemetry older than 60 days is automatically purged.
19. Discord, orchestration, quota/routing, provider adapters, and SQLite run in one daemon process for the MVP.
20. Daemon resource tests/measurements verify the configured RAM/CPU targets under a representative idle workload.
## 18. Design principles

- **Free-first, not free-only.** Free capacity is preferred, but the system remains extensible.
- **Never confuse estimate with truth.** Quota dashboards and local estimates are explicitly labeled.
- **No silent spending.** Paid inference is an explicit policy decision.
- **Agent runtime stays separate.** OpenCode performs coding; the control plane manages access, routing, and safety.
- **Provider details stay behind adapters.** Free-tier APIs change often.
- **Security before convenience.** Discord is an untrusted network boundary; only authorized users reach the local agent.
