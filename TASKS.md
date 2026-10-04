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

Managed by the daemon (T037): the controller launches `opencode serve --hostname 127.0.0.1 --port <opencode.server_port>` with a random Basic-auth password; do not run it by hand.

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

and other OpenRouter free variants (model IDs ending in `:free`). Paid OpenRouter models are never supported.

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

## T036 — LLM gateway

**Status:** DONE (feat/llm-gateway)
**Depends on:** T031, T035

Key-holding loopback gateway in the daemon (`app/gateway/server.py`): bearer-token auth, allowlisted free models only (`AppConfig.gateway_models()`), body-key filtering, exact upstream URL per provider, error passthrough with redaction, cooldown recording, one usage row per upstream request.

Acceptance:
- A model outside the allowlist is rejected with 403 and never sent upstream.
- The real key is used only in the upstream `Authorization` header.

## T037 — Managed opencode serve

**Status:** DONE (feat/llm-gateway)
**Depends on:** T003, T036

The daemon launches and supervises `opencode serve` (`app/opencode/server.py`) with an allowlisted environment (no keys), inline `OPENCODE_CONFIG_CONTENT` (gateway as the only provider, pinned `model`/`small_model`), a random Basic-auth password, restart with backoff, and refusal to start while OpenCode has stored credentials.

Acceptance:
- The served process environment holds no provider key or bot token.
- A crash is followed by a restart and a healthy endpoint.

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

# Phase 5 — Free-only routing

## T050 — Implement candidate generation

**Status:** TODO
**Depends on:** T031, T032, T034, T040

Generate provider/model candidates based on:

- enabled flag
- capabilities
- current health
- quota state

Acceptance:
- Candidate list is deterministic for the same inputs.

## T051 — Implement free-only provider order

**Status:** TODO
**Depends on:** T050

Default order:

```text
1. OpenRouter free
2. Gemini free
3. Groq free
4. other free
(no paid step)
```

Acceptance:
- If OpenRouter is exhausted and Gemini has capacity, Gemini is selected.
- If all free candidates are unavailable, the task stops with `FREE_CAPACITY_EXHAUSTED`.

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

## T053 — Free-only guardrail

**Status:** TODO
**Depends on:** T051

Make paid inference impossible rather than merely disabled:

```text
no paid modes, budgets, approvals, or paid policies in config (rejected at load)
OpenRouter models must be openrouter/free or end in :free (rejected at load otherwise)
cost class has only FREE; the SQLite ledger rejects PAID rows
```

Acceptance:
- A config containing any paid setting fails to load with a clear error.
- A non-free OpenRouter model ID fails to load.
- Paid usage cannot be recorded, even by raw SQL.

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
8. When every free candidate is exhausted, stop with `FREE_CAPACITY_EXHAUSTED`.

Acceptance:
- Simulated OpenRouter 429 causes Gemini free candidate to run.
- Simulated Gemini 429 causes Groq candidate to run.
- Simulated model-specific context failure switches to a compatible model.
- All attempts are persisted in order.
- Exhausting every free candidate stops the task; nothing is paid for.

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

**Status:** DONE via 127.0.0.1 binding + Basic auth (T037).

Keep OpenCode on localhost. If private remote administration is added, use Tailscale/private networking and authentication rather than port forwarding.

Acceptance:
- OpenCode is not publicly reachable from the Internet.

## T072 — Least-privilege OS account

**Status:** DONE
**Depends on:** T002

Run the service under a non-admin account whenever practical.

Done: OpenCode (`opencode serve` and every `opencode run --attach`) runs as the dedicated Linux user `opencode.os_user.name` (default `brokeoc`) through `sudo -n -u <name> -- <exact argv>` with a generated narrow sudoers rule (`python -m app --print-sudoers`; the four commands opencode, pkill, test, head, as that user only). The environment is an exact allowlist. Processes are killed across users with `pkill` (per-session and a sweep at start/stop/every serve reap, N1). A startup self-check (`python -m app --check-os-user`) fails closed (exit 3) when the user can read the daemon home, secrets, database or `/proc` environ, when WSL interop is enabled, or when sudo is not set up. N9 (no spawn while the managed serve is down) and N10 (restart instruction in the fatal message) are included. Mode `none` is for tests and dev only. Setup: docs/SETUP-discord-opencode.md Part C6.

Acceptance:
- The agent cannot read the secrets file, the daemon home or the daemon's `/proc/<pid>/environ`, and cannot write system directories.

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
- no candidate available
- request-time 429 triggers next candidate
- model-specific error switches to the next configured model for the same provider
- an unlisted model is never selected
- Gemini Flash 3.8 falls back only to configured Flash 3.7, then leaves Gemini
- fallback chain stops at max attempts
- exhausting all free candidates stops the task (no paid outcome exists)

## T082 — End-to-end local test

**Status:** TODO
**Depends on:** T022, T061, T081

Use a disposable Git repository.

Also verify these gateway assumptions against real services and OpenCode (each one currently rests on documentation only):

- G1. `@ai-sdk/openai-compatible` sends `Authorization: Bearer <apiKey>` to `POST {baseURL}/chat/completions`; streaming is SSE and the final chunk may carry `usage`.
- G2. Gemini's OpenAI-compatible endpoint is `.../v1beta/openai/chat/completions` with Bearer auth; error body shapes.
- G3. OpenRouter and Groq use `{base_url}/chat/completions` with Bearer auth.
- G4. `opencode run --attach` authenticates to a password-protected server via `OPENCODE_SERVER_PASSWORD` / `OPENCODE_SERVER_USERNAME` in its own environment.
- G5. The `limit` field of a custom-provider model is optional.
- G6. Stored credentials live at `$XDG_DATA_HOME/opencode/auth.json` (default `~/.local/share/opencode/auth.json`), a JSON object keyed by provider id.
- G7. `GET /config` returns the merged config including `enabled_providers` and `provider`; `OPENCODE_CONFIG_CONTENT` outranks global and project files.
- G8. `{env:` / `{file:` substitution also applies to `OPENCODE_CONFIG_CONTENT` (model ids with braces are rejected at config load).
- Pinning only `model` and `small_model` is enough (agent models do not override `--model`); revisit per-agent pinning here.

OS-user live checks (T072). These are the only things that cannot be verified offline:

- L1. `visudo -c` passes; `sudo -n -u brokeoc -- opencode --version` needs no password; `sudo -n true` fails.
- L2. env_keep passes exactly the list: `OPENCODE_SERVER_USERNAME=x FOO=y sudo -n -u brokeoc -- /usr/bin/head -c 4096 /proc/self/environ | tr '\0' '\n'` shows the kept vars and no FOO. The daemon's serve passes the anonymous-401 check and the gateway-only `GET /config`.
- L3 (S1/S2). During a task, `ps -o pid,ppid,sid,user,cmd -u brokeoc` shows opencode's sid == the sudo pid. After /cancel and a run timeout, no `opencode run` is left. After daemon stop, `pgrep -u brokeoc` is empty (N1).
- L4. As brokeoc (`sudo -u brokeoc bash`, admin password), these must all be denied: `cat ~<you>/.config/broke-ai-coder/secrets.env`, `cat /proc/<daemon pid>/environ`, `ls ~<you>`, `ls /mnt/c`, and running any `.exe`.
- L5. Agent-created files in /workspace are group brokeai with g+rw (default ACL). The daemon user's `git status` works, and the agent can commit (safe.directory).
- L6. A planted entry in brokeoc's auth.json makes startup fail with the stored-credentials message, and per-task the task fails `OPENCODE_UNSAFE_CONFIG`.
- L7 (W1). After the wsl.conf change and `wsl --shutdown`, no enabled `WSLInterop*` entry remains, including with systemd on.
- L8. sudo journal entries contain prompts (documented, R1f).
- L9. OpenCode's shell tool works with SHELL=/bin/bash from passwd. OpenCode's provider package download into /home/brokeoc/.cache works.
- L10 (S4/B8). serve's cwd is server_directory and run's cwd is the project directory.
- L11 (S5). The auth.json path under sudo env_reset is the default XDG path.

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

Verify that:

- the shipped `config.example.yaml` contains no paid settings and loads;
- adding any paid setting or a non-free OpenRouter model makes config loading fail;
- with every free provider exhausted, `/code` reports `FREE_CAPACITY_EXHAUSTED` and no request is sent;
- the ledger contains no `PAID` rows after the end-to-end test.

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

## T114 — Web dashboard

**Status:** BACKLOG

Display:

- quota history
- routing events
- provider health
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
[ ] Paid inference is impossible (config, router, and ledger)
[ ] Sensitive actions require approval
[ ] Secrets are protected
[ ] Restart/recovery works
[ ] End-to-end test passes
[ ] Zero-spend test passes
```
