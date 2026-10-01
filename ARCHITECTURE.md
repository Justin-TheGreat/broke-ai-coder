# ARCHITECTURE.md — System Architecture

> Version: 0.1
> Date: 2026-09-30

## 1. Architecture overview

```text
┌───────────────────────────────────────────────────────────────┐
│                         iPhone                               │
│                      Discord App                             │
└──────────────────────────────┬────────────────────────────────┘
                               │ Discord interactions
                               ▼
┌───────────────────────────────────────────────────────────────┐
│                     Discord Bot Service                       │
│  Slash commands • auth • buttons • message formatting        │
└──────────────────────────────┬────────────────────────────────┘
                               │ local IPC / HTTP
                               ▼
┌───────────────────────────────────────────────────────────────┐
│                   Agent Orchestrator                          │
│                                                               │
│  Task Manager ─ Session Manager ─ Approval Manager            │
│        │                    │                                 │
│        └────────────┬───────┘                                 │
│                     ▼                                         │
│              Quota / Model Router                             │
│                     │                                         │
│        ┌────────────┼──────────────┐                          │
│        ▼            ▼              ▼                          │
│   Provider       Provider       Provider ...                  │
│   adapters       adapters       adapters                      │
└────────┬───────────────────────────────────────┬───────────────┘
         │                                       │
         │ selected provider/model              │ DB
         ▼                                       ▼
┌──────────────────────────────┐       ┌─────────────────────────┐
│       OpenCode Runtime       │       │         SQLite          │
│                              │       │ tasks/sessions/quota   │
│ opencode serve               │       │ approvals/events/usage │
│ + opencode run --attach      │       └─────────────────────────┘
└──────────────┬───────────────┘
               │ LLM API
       ┌───────┼────────┐
       ▼       ▼        ▼
 OpenRouter  Gemini    Groq
```

## 2. Deployment topology

Recommended MVP deployment:

```text
Windows PC
├── WSL2 Ubuntu
│   ├── agent-controller
│   ├── Discord bot
│   ├── SQLite
│   ├── opencode serve :4096 (localhost only)
│   └── project repositories
│
└── Windows host
    └── Tailscale (optional, for private remote networking)

iPhone
└── Discord app
```

The Discord bot does not need inbound Internet access from the home PC if it uses the Discord Gateway connection. The PC makes outbound connections to Discord and LLM providers.

### 2.1 Single-daemon rule

For MVP, all controllable control-plane components run inside one process named `agent-controller`:

```text
agent-controller process
├── Discord bot / interaction handler
├── Agent orchestrator
├── Quota manager
├── Model router
├── Provider adapters
├── Optional LLM gateway/proxy
└── SQLite connection / ledger
```

This avoids the RAM/CPU and operational overhead of separate microservices. Use `asyncio` tasks and bounded queues rather than spawning extra worker services.

**Resource budget applies only to this daemon.** OpenCode, user project builds/tests, Docker workloads, WSL2/Windows baseline, Tailscale, and remote LLM inference are explicitly excluded.

Target budgets:

```text
Idle RAM:        < 250 MB
Normal RAM:      < 512 MB
Idle CPU:        < 2% sustained
Normal CPU:      < 10% controller workload
```

## 3. Why the control plane exists

OpenCode already knows how to run a coding agent. The missing control-plane functions are:

```text
OpenCode:
  "How do I perform this coding task?"

Control plane:
  "Which provider can I use right now?"
  "Is that provider free?"
  "How much capacity remains?"
  "Can this task spend money?"
  "Who is allowed to start it?"
  "Does this shell action need approval?"
  "What should Discord display?"
```

Keeping those responsibilities separate prevents provider/quota logic from becoming tightly coupled to OpenCode internals.

## 4. Component responsibilities

### 4.1 Discord Bot

Responsibilities:

- register slash commands
- authenticate Discord users
- enqueue coding tasks
- display progress
- display provider/quota state
- render approval buttons
- handle cancel/status commands

Must not:

- contain provider API keys in source
- execute shell commands directly
- select paid models by itself

### 4.2 Agent Orchestrator

Responsibilities:

- create task/session records
- call router
- start/attach OpenCode
- stream/collect status
- handle cancellation
- call approval manager
- translate agent events into Discord messages

Suggested implementation: Python `asyncio` service.

### 4.3 Quota Manager

Responsibilities:

- retrieve provider quota data
- persist snapshots
- calculate local estimates when exact values are unavailable
- normalize quota units
- track reset timestamps
- classify confidence

Important rule:

```text
No generic quota assumption may be applied to a provider.
```

Each provider adapter defines what is authoritative.

### 4.4 Model Router

Responsibilities:

- discover candidate models
- filter by capability
- enforce free/paid policy
- evaluate quota state
- evaluate provider health
- return the next candidate

Pure decision interface:

```python
Decision route(RouteRequest request)
```

The router should not invoke OpenCode or Discord directly.

### 4.5 Provider Adapter

Common interface:

```python
class ProviderAdapter:
    async def list_models(self) -> list[ModelCapability]: ...
    async def get_quota(self) -> list[QuotaRecord]: ...
    async def health_check(self) -> ProviderHealth: ...
    async def classify_error(self, response) -> ErrorClass: ...
```

Provider-specific implementations handle:

- credentials
- endpoints
- model IDs
- rate-limit headers
- dashboard/API quota discovery
- error parsing

### 4.6 OpenCode Runtime

Run as a local headless server:

```bash
opencode serve --hostname 127.0.0.1 --port 4096
```

The orchestrator can run/attach tasks using the documented CLI:

```bash
opencode run \
  --attach http://127.0.0.1:4096 \
  --model <provider/model> \
  "<prompt>"
```

This keeps OpenCode responsible for the coding-agent loop while the orchestrator chooses the provider/model before each task.

## 5. Routing algorithm

### 5.1 Candidate object

```python
@dataclass
class Candidate:
    provider: str
    model: str
    cost_class: Literal["FREE", "PAID"]
    capabilities: set[str]
    quota_confidence: Literal["EXACT", "ESTIMATED", "UNKNOWN", "EXHAUSTED", "COOLDOWN"]
    remaining_requests: int | None
    remaining_tokens: int | None
    health: Literal["HEALTHY", "DEGRADED", "DOWN"]
    priority: int
```

### 5.2 Selection pseudocode

```python
def choose_candidate(req, candidates, policy):
    eligible = []

    for c in candidates:
        if c.health not in {"HEALTHY", "DEGRADED"}:
            continue
        if not supports(c, req.required_capabilities):
            continue
        if c.quota_confidence in {"EXHAUSTED", "COOLDOWN"}:
            continue
        if not policy.allows(c.cost_class):
            continue
        if not within_safety_threshold(c, req):
            continue
        eligible.append(c)

    free = [c for c in eligible if c.cost_class == "FREE"]

    if free:
        return min(free, key=free_candidate_sort_key)

    paid = [c for c in eligible if c.cost_class == "PAID"]
    if not paid:
        raise NoEligibleProvider()

    if not policy.paid_requires_approval:
        return min(paid, key=paid_candidate_sort_key)

    raise PaidApprovalRequired(paid)
```

`free_candidate_sort_key` is `(provider priority, configured model_order index)` and nothing else. Capability, quota confidence, remaining capacity, and health act only as the eligibility filters above; they must never reorder candidates (see §7.1 and SPEC §6.3).

### 5.3 Request-time fallback / model switching

Preflight quota checks are advisory. A provider can still reject a request after it is sent because quota/rate limits can change between the preflight check and the actual inference. The controller therefore needs a **request-time fallback loop**.

Example:

```text
Task
  ↓
Preflight router
  ↓
OpenRouter / openrouter/free
  ↓
HTTP 429 / quota_exceeded
  ↓
record attempt + mark candidate cooldown/exhausted
  ↓
re-route same task
  ↓
Gemini Flash
  ↓
HTTP 429
  ↓
record attempt + cooldown
  ↓
Groq model X
  ↓
success
```

The request-time fallback engine must: 

1. Receive the provider/model error and classify it.
2. Treat quota/rate-limit/provider-unavailable errors as fallback candidates when safe.
3. Update quota and cooldown state before selecting the next candidate.
4. Exclude the failed provider/model for the remainder of the retry chain (or until its cooldown expires).
5. Select the next eligible **provider/model pair**, not merely another provider.
6. Reuse the existing OpenCode session when the CLI/server contract allows continuing the session with `--session` and a new `--model`; otherwise create a controlled continuation with the saved session context. OpenCode documents `--session` and `--model` for `opencode run`.
7. Never silently cross from FREE to PAID. A paid candidate pauses in `WAITING_APPROVAL` unless policy explicitly permits it.
8. Stop after a configurable maximum number of fallback attempts to avoid loops.

### 5.4 Error classes and fallback behavior

```text
429 / rate_limit / quota_exceeded
    → mark exhausted or cooldown
    → try next eligible candidate

408 / timeout / transient network
    → retry same candidate only within retry budget
    → then fallback if still failing

5xx / provider unavailable
    → cooldown candidate/provider
    → fallback

400 / invalid request
    → usually do not retry same request
    → if error is model-specific (for example context/output capability), try a compatible alternative model

401 / 403
    → do not rotate repeatedly
    → mark credential/configuration failure
    → fallback only to other providers with valid credentials
```

### 5.5 Important idempotency rule

A model request failure is not always proof that the provider did not process the request. A timeout can occur after the provider accepted the request. Therefore the controller must distinguish:

```text
SAFE_TO_REPLAY
UNSAFE_OR_UNKNOWN_TO_REPLAY
```

For `UNSAFE_OR_UNKNOWN_TO_REPLAY`, record the attempt and prefer continuing from OpenCode/session state rather than blindly duplicating side-effectful agent/tool steps.

### 5.6 Fallback chain state

Persist a per-task attempt chain:

```text
task_id
attempt_no
provider
model
started_at
finished_at
error_class
http_status
retry_after
quota_snapshot_id
status
```

Example:

```text
#1 openrouter/free     → 429 rate_limit → FALLBACK
#2 gemini/gemini-flash → 429 quota     → FALLBACK
#3 groq/gpt-oss        → success       → COMPLETE
```

## 6. Quota architecture

```text
Provider Adapter
      │
      ├── authoritative response headers/API
      │
      ├── provider dashboard metadata
      │
      └── observed 429 / retry-after
      ▼
Quota Normalizer
      ▼
Quota Snapshot Store
      ▼
Quota Estimator
      ▼
Router
```

### 6.1 Exact sources

Examples:

- Groq: response headers include remaining request/token values and reset information.
- OpenRouter: provider/account APIs and activity/limit information where available.
- Gemini: active rate limits are visible in AI Studio; runtime behavior/429s are authoritative for actual availability.

The system must not claim an exact remaining count when the provider only exposes a dashboard or generic limit.

### 6.2 Local accounting

Every request produces a usage event:

```json
{
  "provider": "gemini",
  "model": "<model-id>",
  "request_id": "provider-request-id-if-known",
  "input_tokens": 18000,
  "output_tokens": 3500,
  "status": "success",
  "timestamp": "..."
}
```

The estimator calculates a best-effort remaining value when the provider permits this.

## 7. Provider policy registry

Use declarative configuration rather than embedding provider order or model order in code.

```yaml
provider_order:
  - openrouter-free
  - gemini-free
  - groq-free
  - other-free
  - openrouter-paid

providers:
  - id: openrouter-free
    provider: openrouter
    model: openrouter/free
    cost_class: FREE
    priority: 10
    enabled: true

  - id: gemini-free
    provider: gemini
    model_order:
      - <gemini-flash-3.8-api-id>
      - <gemini-flash-3.7-api-id>
    cost_class: FREE
    priority: 20
    enabled: true

  - id: groq-free
    provider: groq
    model_order:
      - <groq-model-x-api-id>
      - <groq-model-y-api-id>
    cost_class: FREE
    priority: 30
    enabled: true

  - id: openrouter-paid
    provider: openrouter
    model_order:
      - <configured-paid-model-id>
    cost_class: PAID
    priority: 100
    enabled: true
    requires_approval: true
```

`model_order` is a **hard allowlist and ordered fallback chain** for that provider. Provider adapters may discover other models for metadata/health purposes, but the router must never select a model outside the configured list.

For provider-level routers such as `openrouter/free`, the router treats the configured router ID as a single candidate. OpenRouter's dynamic selection of an underlying free model remains inside OpenRouter unless an explicit provider-supported underlying-model restriction is configured.

### 7.1 Ordered routing algorithm

```text
for provider in provider_order:
    if provider disabled: continue

    for model in provider.model_order (or provider-level router model):
        if not allowlisted(model): continue
        if not capability_compatible(model): continue
        if not quota_eligible(model): continue
        if not healthy(model): continue

        send request

        if success:
            return
        if retryable model/quota/availability failure:
            record failure
            continue to next configured model
        if unknown outcome:
            reconcile safely; do not blindly replay

    continue to next provider
```

The implementation must preserve the exact configured ordering. Quota, capability, and health checks can remove an ineligible candidate, but cannot promote a later model ahead of an eligible earlier model. An unlisted model is never inserted as an ad-hoc fallback.

## 8. Discord event flow

### New task

```text
Discord /code
   ↓
Authorize user
   ↓
Create task
   ↓
Resolve project/repo
   ↓
Estimate task requirements
   ↓
Router selects candidate
   ↓
Start / attach OpenCode session
   ↓
Stream progress
   ↓
Complete / approval / failure
   ↓
Persist result
```

### Provider/model failure

```text
OpenCode request fails
        ↓
Provider adapter classifies error
        ↓
Retryable quota/model/availability failure?
      /                 \
    yes                  no
    ↓                     ↓
record + cooldown      fail/reconcile
    ↓
next configured model for same provider
    ↓ if none eligible
next provider in provider_order
    ↓
resume safely when possible
```

The fallback chain is strictly policy-bounded. An unlisted model is never inserted as an ad-hoc fallback.

The system should not restart a side-effectful tool step solely because the model request failed. Resume from OpenCode session state when possible.

## 9. Approval architecture

```text
Agent requests sensitive action
          ↓
Approval Manager
          ↓
Persist pending approval
          ↓
Discord message + buttons
      ┌───┴───┐
    Approve  Deny
      ↓        ↓
 continue    reject
```

Approval tokens must be one-time and tied to:

```text
approval_id
task_id
session_id
discord_user_id
action_hash
expires_at
```

## 10. Data model

### projects

```text
id
name
working_directory
git_remote(optional)
policy_id
created_at
updated_at
```

### tasks

```text
id
project_id
discord_user_id
prompt
status
session_id
selected_provider
selected_model
cost_class
created_at
started_at
finished_at
exit_code
error_class
```

### provider_model_policy

```text
id
provider
model
priority
enabled
cost_class
created_at
updated_at
```

`priority` represents the explicit within-provider order. A lower numeric value is attempted first. A model absent from configuration is not eligible for routing.

### quota_snapshots

```text
id
provider
model
window
limit
used
remaining
unit
confidence
reset_at
observed_at
source
```

### approvals

```text
id
task_id
session_id
action_type
action_payload_hash
status
requested_at
expires_at
resolved_at
resolved_by
```

### usage_events

```text
id
task_id
provider
model
request_id
input_tokens
output_tokens
estimated_cost_usd
cost_class
status
timestamp
```

### resource/retention rules

The daemon must not accumulate unbounded memory while streaming OpenCode events. Discord progress messages and logs must be bounded/truncated, and the SQLite writer must use a bounded async queue.

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

One row represents one provider/model attempt in the fallback chain, including failed requests.

## 11. Security boundary

```text
                 UNTRUSTED
Discord Internet
       │
       ▼
Discord Bot
       │  authorization
       ▼
Agent Orchestrator
       │  local-only API
       ▼
OpenCode
       │
       ▼
OS shell / repositories
                 TRUSTED
```

API keys are stored only on the trusted PC environment.

Recommended network rules:

- OpenCode listens on `127.0.0.1`.
- Discord bot uses outbound Gateway/HTTPS connections.
- Tailscale may be used for private administration, not public exposure.
- No router port forwarding.

## 11.1 Daemon resource observability

The controller should expose lightweight self-metrics:

```text
process_rss_bytes
process_cpu_percent
queue_depth
event_dropped_count
sqlite_db_bytes
last_cleanup_at
last_vacuum_at
```

These metrics describe only the controller process and its SQLite file. They are not intended to measure OpenCode or project workload resource usage.

## 12. Failure modes

| Failure | System behavior |
|---|---|
| Discord disconnected | Existing OpenCode task continues; state persisted |
| Bot restarted | Reload task state from SQLite |
| OpenCode restarted | Mark running session uncertain and require recovery logic |
| Provider 429 | Update quota/cooldown and try next eligible provider |
| Provider 401 | Disable provider until credential is fixed |
| All free exhausted | Report `FREE_CAPACITY_EXHAUSTED`; do not spend |
| Paid candidate exists | Ask for approval unless policy explicitly permits |
| Tool approval times out | Deny action |
| PC offline | Discord cannot execute; bot should report unavailable |

## 13. Suggested repository layout

```text
broke-ai-coder/
├── app/
│   ├── bot/
│   ├── orchestrator/
│   ├── router/
│   ├── quota/
│   ├── providers/
│   │   ├── base.py
│   │   ├── openrouter.py
│   │   ├── gemini.py
│   │   └── groq.py
│   ├── opencode/
│   ├── approvals/
│   ├── db/
│   └── config/
├── tests/
├── scripts/
├── config.example.yaml
├── SPEC.md
├── ARCHITECTURE.md
└── TASKS.md
```

## 14. Technology recommendation

MVP:

```text
Python 3.12+
asyncio
Discord.py or equivalent Discord library
httpx
Pydantic
SQLite + SQLAlchemy/SQLModel
PyYAML
structlog or standard structured logging
```

The project should keep external provider SDK usage minimal. HTTP adapters are often easier to test and less coupled to fast-changing provider SDKs.

## 15. Future architecture

Potential additions after MVP:

- web dashboard
- automatic provider/model discovery
- multiple PC workers
- local Ollama/llama.cpp candidate
- GitHub PR agent mode
- scheduled coding tasks
- per-project budgets
- per-project allowlists
- richer OpenCode event streaming
- token-cost prediction before paid approval
- provider performance history

These should not complicate the MVP interfaces.

## 16. References checked on 2026-09-30

- OpenCode providers: https://opencode.ai/docs/providers/
- OpenCode CLI / headless run: https://opencode.ai/docs/cli/
- OpenCode server: https://opencode.ai/docs/server/
- OpenRouter free models: https://openrouter.ai/openrouter/free/
- OpenRouter pricing/free plan: https://openrouter.ai/pricing
- Google Gemini API rate limits: https://ai.google.dev/gemini-api/docs/rate-limits
- Google Gemini API billing: https://ai.google.dev/gemini-api/docs/billing/
- Groq rate limits: https://console.groq.com/docs/rate-limits
- Discord application commands: https://docs.discord.com/developers/docs/interactions/slash-commands

Quota numbers and available free models must be re-validated at runtime because provider policies change.
