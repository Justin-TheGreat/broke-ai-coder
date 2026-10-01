# TASKS.md — Implementation Plan

> Version: 0.1
> Date: 2026-09-30
> Target: MVP first, then reliability/security hardening

## 0. Definition of done

The project is MVP-complete when an authorized user can open Discord on an iPhone, run `/code`, have OpenCode execute the task on the home PC using a free provider when possible, receive progress/results, and see an explicit error/approval when no free capacity is available.

---

# Phase 0 — Repository and environment

## T001 — Create repository skeleton

**Status:** TODO
**Depends on:** —

Create:

```text
app/
tests/
scripts/
config.example.yaml
SPEC.md
ARCHITECTURE.md
TASKS.md
README.md
```

Acceptance:
- `python -m app` or equivalent starts successfully.
- Basic lint/test commands exist.

## T001A — Define single-daemon runtime

**Status:** TODO
**Depends on:** T001

Implement `agent-controller` as a single Python `asyncio` process containing Discord handling, orchestration, quota routing, provider adapters, optional gateway logic, and SQLite access.

Acceptance:
- No Redis/Postgres/RabbitMQ is required.
- One command starts the controller daemon.
- Provider calls and Discord handlers do not spawn long-lived helper daemons.
- Internal queues are bounded.

## T002 — Set up WSL2 runtime

**Status:** TODO
**Depends on:** T001

Install/configure:

- Python
- git
- Node.js if required by OpenCode installation
- OpenCode
- tmux (optional but recommended)
- SQLite

Acceptance:

```bash
opencode --version
python --version
git --version
```

all work.

## T003 — Configure OpenCode headless server

**Status:** TODO
**Depends on:** T002

Run/configure:

```bash
opencode serve --hostname 127.0.0.1 --port 4096
```

Enable server authentication if the final deployment ever exposes the HTTP service beyond localhost.

Acceptance:
- `/global/health` is reachable locally.
- `opencode run --attach ...` works.

---

# Phase 1 — Discord control plane

## T010 — Create Discord application/bot

**Status:** TODO
**Depends on:** T001

Create a Discord application, bot, and slash commands.

Required initial commands:

```text
/code
/status
/usage
/models
/cancel
/sessions
/approve
/deny
```

Acceptance:
- Commands appear on iPhone Discord.
- Only the configured test user can use them.

## T011 — Implement Discord authorization middleware

**Status:** TODO
**Depends on:** T010

Config:

```yaml
discord:
  allowed_user_ids: []
  allowed_guild_ids: []
  allowed_channel_ids: []
```

Acceptance:
- Unauthorized user receives no agent execution access.
- Authorization failures are logged without leaking secrets.

## T012 — Implement `/code` task creation

**Status:** TODO
**Depends on:** T011, T003

Create a task and return an immediate acknowledgement.

Acceptance:
- Discord receives a response within the interaction deadline.
- Work continues asynchronously.

## T013 — Implement progress/result formatting

**Status:** TODO
**Depends on:** T012

Convert orchestrator events into compact Discord messages.

Acceptance:
- Start/provider selection/progress/final result are visible.
- Long logs are truncated or attached safely.

---

# Phase 2 — Core orchestration

## T020 — Define task/session state machine

**Status:** TODO
**Depends on:** T001

Implement:

```text
QUEUED
ROUTING
RUNNING
WAITING_APPROVAL
SUCCEEDED
FAILED
CANCELLED
```

Acceptance:
- Invalid state transitions are rejected.
- State survives process restart.

## T021 — SQLite schema and migrations

**Status:** TODO
**Depends on:** T020

Create tables from `ARCHITECTURE.md`.

Acceptance:
- Fresh database initializes automatically.
- Schema version is tracked.
- SQLite WAL mode and busy timeout are configured.
- Historical telemetry retention is set to 60 days.

## T021A — Implement 60-day SQLite retention and cleanup

**Status:** TODO
**Depends on:** T021

Implement a daily cleanup job that removes historical records older than 60 days from:

```text
usage_events
provider_events
fallback_attempts
quota_snapshots
closed task/session history
```

Requirements:
- Use a single cleanup cutoff timestamp per run.
- Protect active/resumable sessions from deletion.
- Use batched deletes for high-volume tables.
- Run `PRAGMA wal_checkpoint(TRUNCATE)` or equivalent controlled maintenance when appropriate.
- Run `VACUUM` only during a low-activity maintenance window, not after every cleanup.

Acceptance:
- Synthetic records older than 60 days are removed.
- Active records remain.
- Cleanup is idempotent.
- Database size is observable.

## T022 — OpenCode executor

**Status:** TODO
**Depends on:** T003, T020

Implement a wrapper around:

```bash
opencode run --attach http://127.0.0.1:4096 --model <provider/model> "<prompt>"
```

Requirements:

- async process handling
- timeout handling
- cancellation
- exit-code capture
- structured output where supported

Acceptance:
- A simple `/code` task edits a test repository and returns success.

## T023 — Session resume

**Status:** TODO
**Depends on:** T022, T021

Persist OpenCode session identifiers.

Acceptance:
- `/sessions` lists recent sessions.
- A follow-up can continue an existing session.

---

## T024 — Daemon resource budget and self-monitoring

**Status:** TODO
**Depends on:** T001A, T021A

Measure and enforce engineering targets for the controllable daemon only:

```text
Idle RAM < 250 MB
Normal RAM < 512 MB
Sustained idle CPU < 2%
Normal controller CPU < 10%
```

Do not include OpenCode or project build/test processes in these measurements.

Requirements:
- Add lightweight RSS/CPU/queue-depth metrics.
- Bound in-memory event buffers.
- Avoid retaining full OpenCode logs in memory.
- Add a repeatable local benchmark script.

Acceptance:
- 30-minute idle benchmark stays within target or documents the specific exception.
- Representative Discord/task workload stays within the normal RAM/CPU target excluding child OpenCode process usage.

# Phase 3 — Provider abstraction

## T030 — Define provider interfaces

**Status:** TODO
**Depends on:** T021

Implement interfaces for:

```python
list_models()
get_quota()
health_check()
classify_error()
```

Acceptance:
- Router depends only on interfaces, not provider-specific SDKs.

## T031 — Implement OpenRouter adapter

**Status:** TODO
**Depends on:** T030

Support:

```text
openrouter/free
```

and selected paid models behind policy control.

Acceptance:
- API key loads from environment/secret storage.
- Free router can be selected by the routing engine.
- Provider errors are classified.

## T032 — Implement Gemini adapter

**Status:** TODO
**Depends on:** T030

Support a configured Gemini free-capable model.

Important:
- Do not hard-code today's free quota.
- Store configured limits separately from runtime observations.
- Label local remaining estimates as `ESTIMATED`.

Acceptance:
- Successful request through OpenCode.
- 429 handling is tested.

## T034 — Implement Groq adapter

**Status:** TODO
**Depends on:** T030

Support at least one current free-tier coding-capable model.

Acceptance:
- Response rate-limit headers are parsed when present.
- Remaining/reset values are stored.

## T035 — Provider health/cooldown manager

**Status:** TODO
**Depends on:** T031, T032, T034

Implement temporary provider cooldowns after:

- 429
- repeated network failure
- service unavailable

Acceptance:
- Router skips a provider during cooldown.
- Cooldown expires automatically.

---

# Phase 4 — Quota manager

## T040 — Normalize quota schema

**Status:** TODO
**Depends on:** T030

Support dimensions:

```text
requests_per_minute
requests_per_day
tokens_per_minute
tokens_per_day
tokens_per_hour
spend_usd
```

Not every provider will expose every dimension.

Acceptance:
- Missing dimensions are represented as null, not zero.

## T041 — Exact quota ingestion where available

**Status:** TODO
**Depends on:** T031, T032, T034, T040

Implement provider-specific exact sources where reliable.

Acceptance:
- Every stored quota snapshot includes `source` and `confidence`.

## T042 — Local usage accounting

**Status:** TODO
**Depends on:** T040

Record every request and token count returned by providers/OpenCode when available.

Acceptance:
- `/usage` displays today's observed usage.

## T043 — Estimated quota engine

**Status:** TODO
**Depends on:** T042

For providers without authoritative remaining counters, estimate remaining capacity from:

```text
configured limit
- observed usage
```

Attach:

```text
confidence = ESTIMATED
```

Acceptance:
- UI never labels an estimate as exact.

## T044 — Quota refresh job

**Status:** TODO
**Depends on:** T041-T043

Refresh provider quota metadata periodically, with backoff.

Do not poll aggressively.

Acceptance:
- `/usage` stays reasonably fresh.
- Provider rate limits are not harmed by quota checks.

---

# Phase 5 — Smart free-first routing

## T050 — Implement candidate generation

**Status:** TODO
**Depends on:** T031, T032, T034, T040

Generate provider/model candidates based on:

- enabled flag
- capabilities
- current health
- quota state
- cost class

Acceptance:
- Candidate list is deterministic for the same inputs.

## T051 — Implement free-first policy

**Status:** TODO
**Depends on:** T050

Default order:

```text
1. OpenRouter free
2. Gemini free
3. Groq free
4. other free
5. paid only after policy/approval
```

Acceptance:
- If OpenRouter is exhausted and Gemini has capacity, Gemini is selected.
- If all free candidates are unavailable, the task stops rather than spending.

## T051A — Implement per-provider ordered model allowlists

**Status:** TODO
**Depends on:** T050, T051

Implement declarative, provider-specific model order. This is a hard allowlist, not a preference hint.

Example policy intent:

```yaml
model_order:
  gemini:
    - <gemini-flash-3.8-api-id>
    - <gemini-flash-3.7-api-id>
  groq:
    - <groq-model-x-api-id>
    - <groq-model-y-api-id>
```

Requirements:
- Models are attempted strictly in listed order.
- Unlisted models are never eligible, even when discovered or healthy.
- Discovery can report unlisted models as `DISCOVERED_ONLY` for visibility.
- If model #1 fails with a retryable model/quota/availability error, try model #2.
- After all allowed models for a provider are unavailable/exhausted, move to the next provider.
- The same policy applies to initial routing and request-time fallback.
- Reordering the list changes behavior without code changes.
- Arbitrary model overrides from Discord are rejected unless the model is explicitly allowlisted.

Acceptance:
- Gemini configured as `[Flash 3.8, Flash 3.7]` never invokes any third Gemini model.
- Groq likewise never invokes an unlisted model.
- A simulated Flash 3.8 429 causes Flash 3.7 to be attempted before another Gemini model or another provider.
- `/models` marks configured models `ALLOWED` and other discovered models `DISCOVERED_ONLY`.

## T052 — Capability-aware routing

**Status:** TODO
**Depends on:** T051, T051A

Filter candidates for:

```text
tool calling
structured output
vision
context window
coding/tool reliability metadata
```

Acceptance:
- A task requiring a capability is never routed to a candidate that does not support it.

## T053 — Paid fallback guardrail

**Status:** TODO
**Depends on:** T051

Implement:

```text
FREE_ONLY
FREE_FIRST_NO_PAID
FREE_FIRST_PAID_AFTER_APPROVAL
```

Acceptance:
- Default deployment cannot create paid usage.
- Paid request produces an approval prompt.

## T054 — Request-time provider/model fallback

**Status:** TODO
**Depends on:** T022, T035, T050-T053, T051A

Implement a fallback chain that runs **after the request has already been sent** and the selected provider/model returns a retryable quota/rate-limit/availability error.

Required behavior:

1. Persist the attempt and failure.
2. Parse HTTP status, provider error code, `Retry-After`, and provider request ID when available.
3. Update quota and cooldown state.
4. Exclude the failed provider/model from the active fallback chain.
5. Ask the router for the next compatible provider/model.
6. Continue the same OpenCode session when supported by `--session` + `--model`.
7. Enforce `max_fallback_attempts`.
8. Never switch FREE → PAID without the paid policy/approval gate.

Acceptance:
- Simulated OpenRouter 429 causes Gemini free candidate to run.
- Simulated Gemini 429 causes Groq candidate to run.
- Simulated model-specific context failure switches to a compatible model.
- All attempts are persisted in order.
- Free-only mode never sends a paid request.

## T055 — Safe replay and unknown-outcome handling

**Status:** TODO
**Depends on:** T054, T023

Handle timeouts/network failures where the provider may have accepted the request even though the controller received no response.

Acceptance:
- Unknown outcomes are recorded explicitly.
- No infinite blind replay.
- Side-effectful agent/tool operations are not blindly duplicated.
- All fallback attempts keep the same parent task/session identity.

---

# Phase 6 — Safety and approvals

## T060 — Action interception

**Status:** TODO
**Depends on:** T022

Detect sensitive operations:

```text
git push
rm -rf / /repo-wide-delete
deploy production
package installation
credential changes
```

Acceptance:
- Sensitive operations create an approval request.

## T061 — Discord approval buttons

**Status:** TODO
**Depends on:** T010, T060

Implement:

```text
[Approve]
[Deny]
```

Acceptance:
- Only authorized user can resolve.
- Approval token is one-time and expires.

## T062 — Paid-model approval

**Status:** TODO
**Depends on:** T053, T061

Present:

```text
Provider
Model
Estimated cost
Reason free candidates unavailable
```

Acceptance:
- User must explicitly approve before paid execution.

## T063 — Command cancellation

**Status:** TODO
**Depends on:** T012, T022

Implement `/cancel` with task/session ID.

Acceptance:
- Running task receives cancellation.
- Persistent state becomes `CANCELLED`.

---

# Phase 7 — Security hardening

## T070 — Secret management

**Status:** TODO
**Depends on:** T031, T032, T034

Use environment variables or local secret storage:

```text
OPENROUTER_API_KEY
GEMINI_API_KEY
GROQ_API_KEY
DISCORD_BOT_TOKEN
```

Acceptance:
- No secret appears in Git.
- No secret appears in Discord.
- No secret appears in structured logs.

## T071 — Restrict OpenCode network exposure

**Status:** TODO
**Depends on:** T003

Keep OpenCode on localhost. If private remote administration is added, use Tailscale/private networking and authentication rather than port forwarding.

Acceptance:
- OpenCode is not publicly reachable from the Internet.

## T072 — Least-privilege OS account

**Status:** TODO
**Depends on:** T002

Run the service under a non-admin account whenever practical.

Acceptance:
- Agent cannot modify unrelated system directories as part of normal execution.

## T073 — Log redaction

**Status:** TODO
**Depends on:** T070

Redact known secret patterns and provider authorization headers.

Acceptance:
- Automated test proves configured secrets do not appear in logs.

---

# Phase 8 — Testing

## T080 — Provider adapter unit tests

**Status:** TODO
**Depends on:** T031, T032, T034

Mock:

- success
- 429
- 401
- 5xx
- malformed response
- missing quota headers

## T081 — Router unit tests

**Status:** TODO
**Depends on:** T050-T053

Test:

- free candidate chosen
- exhausted candidate skipped
- capability mismatch skipped
- cooldown respected
- paid approval required
- no candidate available
- request-time 429 triggers next candidate
- model-specific error switches to the next configured model for the same provider
- an unlisted model is never selected
- Gemini Flash 3.8 falls back only to configured Flash 3.7, then leaves Gemini
- fallback chain stops at max attempts
- paid candidate is never selected without approval

## T082 — End-to-end local test

**Status:** TODO
**Depends on:** T022, T061, T081

Use a disposable Git repository.

Test flow:

```text
Discord /code
 -> router
 -> OpenCode
 -> edit file
 -> run test
 -> report result
```

## T083 — Failure-injection test

**Status:** TODO
**Depends on:** T082

Simulate:

```text
OpenRouter 429
Gemini success
```

Expected:

```text
request starts with OpenRouter
429 detected
OpenRouter cooldown
Gemini selected
same user task continues safely
```

## T084 — Zero-spend test

**Status:** TODO
**Depends on:** T053, T082

Verify that with:

```yaml
mode: free-first-no-paid
daily_paid_budget_usd: 0
```

no paid provider can be invoked even if it is the only eligible candidate.

---

# Phase 9 — Operations

## T090 — `/status`

**Status:** TODO
**Depends on:** T012, T021

Show:

```text
PC: online
OpenCode: healthy
Current task: running
Provider: Gemini
Session: abc123
```

## T091 — `/usage`

**Status:** TODO
**Depends on:** T044

Show provider cards such as:

```text
OpenRouter Free  — 38 req remaining (EXACT)
Gemini           — ~120 req remaining (ESTIMATED)
Groq             — 14.2k TPM remaining (EXACT)
Paid spend       — $0.00 / $0.00
```

Do not display a number if the system cannot support the number; use `UNKNOWN`.

## T092 — `/models`

**Status:** TODO
**Depends on:** T050

Show currently configured/known model candidates and their capability metadata.

Also show routing status:

```text
Gemini
  1. Flash 3.8  ALLOWED
  2. Flash 3.7  ALLOWED
  other models DISCOVERED_ONLY (never selectable)
```

## T093 — Health-check command

**Status:** TODO
**Depends on:** T031, T032, T034

Optional admin-only command:

```text
/health
```

Runs safe, low-cost provider checks without initiating a full coding task.

---

# Phase 10 — Documentation and deployment

## T094 — `/health` and daemon resource diagnostics

**Status:** TODO
**Depends on:** T024, T090-T093

Expose controller-only diagnostics for:

```text
RSS memory
CPU usage
SQLite size
SQLite last cleanup
queue depth
provider cooldown counts
```

Acceptance:
- Diagnostics never expose API keys.
- Values clearly state they exclude OpenCode/project child workload.

## T100 — Create `.env.example`

**Status:** TODO
**Depends on:** T070

Include variable names only, never real secrets.

## T101 — Create production-ish startup scripts

**Status:** TODO
**Depends on:** T003, T012

Provide scripts for:

```text
start-opencode
start-bot
stop-bot
health-check
```

## T102 — Optional Windows/WSL auto-start

**Status:** TODO
**Depends on:** T101

Use WSL/systemd or Windows Task Scheduler depending on the chosen deployment style.

Acceptance:
- PC reboot recovery starts the required services without manual intervention.

## T103 — Write README quick start

**Status:** TODO
**Depends on:** all MVP phases

Document:

1. provider account setup
2. API keys
3. Discord app setup
4. OpenCode setup
5. first `/code` test
6. quota/status commands
7. security notes

---

# Phase 10.5 — Operations/maintenance

The MVP daemon owns only the controllable resources. OpenCode resource usage is intentionally not managed by this project.

```text
Controlled:
  Discord bot
  orchestrator
  quota/router
  provider adapters
  gateway
  SQLite

Not controlled/budgeted:
  OpenCode
  child tests/builds
  Docker started by projects
  WSL2/Windows baseline
  remote provider inference
```

# Phase 11 — Optional extensions

## T110 — Add more free provider adapters

**Status:** BACKLOG

Candidates can include other providers with a usable free API tier or credits. The adapter contract is the only required integration point.

## T111 — Local model fallback

**Status:** BACKLOG

Support:

```text
Ollama / llama.cpp
```

This is useful when all hosted free quotas are exhausted.

## T112 — GitHub integration

**Status:** BACKLOG

Possible flow:

```text
Discord
 -> create branch
 -> OpenCode coding task
 -> tests
 -> pull request
```

## T113 — Cost prediction

**Status:** BACKLOG

Estimate paid cost before approval using model pricing and expected input/output tokens.

## T114 — Web dashboard

**Status:** BACKLOG

Display:

- quota history
- routing events
- provider health
- spend
- session history

---

# Recommended implementation order

For the fastest path to a working personal system:

```text
T001
T002
T003
T010
T011
T012
T020
T021
T022
T030
T031
T032
T050
T051
T051A
T053
T013
T061
T070
T071
T080
T081
T082
T084
T090
T091
T103
```

Then add:

```text
T034
T035
T041-T044
T052
T060
T063
T072-T073
T083
T092-T102
```

## MVP milestone checklist

```text
[ ] Discord /code works
[ ] OpenCode runs headlessly
[ ] OpenRouter free works
[ ] Gemini works
[ ] Provider fallback works
[ ] Per-provider model allowlists are enforced
[ ] Quota state is visible
[ ] Paid is blocked by default
[ ] Sensitive actions require approval
[ ] Secrets are protected
[ ] Restart/recovery works
[ ] End-to-end test passes
[ ] Zero-spend test passes
```
