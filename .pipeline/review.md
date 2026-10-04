# Review: key-holding LLM gateway + controller-managed `opencode serve` (feat/llm-gateway, uncommitted on 926b655)

VERDICT: NEEDS WORK

Gate, re-run by the reviewer with `.venv/Scripts/python`:
- `pytest -q`: 1143 passed, 1 xfailed (pre-existing strict xfail).
- `ruff check .`: clean. `ruff format --check .`: 133 files formatted.
- `-m app --check --config config.example.yaml`: config OK.
- Smoke: `-m app --once` with DISCORD_BOT_TOKEN unset and no opencode binary prints
  `opencode executable not found` and exits 3 (gateway started and was torn down cleanly).
  With `opencode.command` pointed at `tests/fakes/fake_opencode.py`, `--once` starts the gateway,
  the managed serve (Basic-auth health 200), a NullFrontend, runs cleanup, and exits 0 with no
  orphaned fake processes.

## Summary

The design does achieve the goal structurally, within the accepted residual risk R1:
- The OpenCode env is an allowlist (`app/opencode/env.py`), for both `serve` and the
  `run --attach` child. Provider keys, the bot token and `OPENCODE_*` cannot reach it. asyncio
  subprocesses use `close_fds=True`, so no inherited descriptors.
- The inline config makes `broke` the only enabled provider. `OPENCODE_CONFIG_CONTENT` outranks
  project files (G7), so a project or agent-written `opencode.json` cannot re-enable another provider.
  `{env:}`/`{file:}` substitution has nothing to grab: model ids reject braces and whitespace,
  provider ids are a fixed set, and the token is urlsafe.
- `auth.json` blocks startup and every task.
- The gateway is solid:
  - Auth runs first (`hmac.compare_digest`), before the body is read.
  - The model must be an exact key of `gateway_models()`. There is no normalization or substitution.
  - The body is filtered by an allowlist, so `models`/`route`/`provider`/`plugins`/`transforms`
    are dropped.
  - Requests go only to `adapter.chat_url()`, with `follow_redirects=False` (3xx becomes 502) and
    `trust_env=False`. No client headers are forwarded.
  - Errors are capped and redacted, and the token is scrubbed.
  - It binds 127.0.0.1 only.
  - Usage rows are FREE and written only by the gateway.
- Lifecycle ordering is right:
  - Services start after recovery and before the workers and frontend.
  - Teardown cancels the workers (and aborts sessions while serve is still up), then stops the
    manager, then the gateway, then closes conn.
  - A failing service start tears down the services already started.
- Tests are meaningful:
  - Real loopback fake serve, MockTransport upstreams, byte-exact SSE splits, a process-tree kill.
  - 14 mutation checks; I spot-checked the reported ones against the code.
- SPEC/ARCHITECTURE/TASKS/docs/config.example.yaml match the code. R1-R4 are stated as specified.
  Docs no longer source `secrets.env` from `~/.bashrc`. It is loaded only in the daemon start command
  (docs/SETUP-discord-opencode.md:145, :238), with a migration row at :272. The remaining
  `source ~/.bashrc` at :166 is the opencode installer PATH line, which is fine.

Two required fixes remain. Neither is a key leak, but both are holes in "structural" free-only:
- The manager accepts a server it did not start as "healthy".
- The gateway forwards provider-hosted (non-function) tools and file parts verbatim.

## REQUIRED FIXES

### 1. The managed serve accepts any healthy server on its port as itself (app/opencode/server.py:192-195, :236-257)
Reproduced with a scratch script:
- A plain HTTP server on 127.0.0.1:P returns `{"healthy": true}` and ignores auth.
- `OpenCodeServerManager.start()` runs with `server_port: P`.
- Result: `START SUCCEEDED against foreign server`.

Why it happens:
- `_wait_healthy` checks `proc.returncode` (still None, because opencode is still booting) before
  probing health.
- The foreign server answers 200, so the manager reports healthy.
- Our own serve then dies on EADDRINUSE. The monitor then loops "restart -> healthy -> immediate
  exit" forever, and every `opencode run --attach` goes to the foreign server.

Why it matters:
- The default `server_port` is 4096. That is exactly where the OLD docs had the user run a tmux
  `opencode serve`, from a shell that sourced `secrets.env` through `.bashrc`, so the server has the
  real keys in its env and no password.
- Today only the per-task GET /config preflight catches this (baseURL/enabled_providers mismatch,
  fail closed). That preflight is documented as "accident detection", not the guarantee.
- There is also a cache window. A crash of our serve followed by a foreign server on the port within
  `small_model_check_ttl_s` (OK is cached) lets a task run on a keyed server.

Fix:
- (a) In `_spawn`, before spawning with a fixed port, bind-test `127.0.0.1:port` with a plain socket
  (no SO_REUSEADDR). If it is in use, raise
  `_ServeFailed(f"port {port} already in use (stop any manually started opencode serve)")`.
- (b) In `_wait_healthy`, once the authenticated health returns healthy, also require that an
  UNauthenticated `GET /global/health` returns 401. That proves a password-protected server. A foreign
  server with a different password already fails the authenticated probe.
- (c) After (a)/(b), invalidate the cached OK server-config check in the orchestrator whenever the
  manager (re)launches. Alternatively, have the executor include a manager launch generation in the
  cache key.
- Tests:
  - occupied fixed port gives StartupError mentioning "already in use";
  - a foreign unauthenticated healthy server on the port is not accepted;
  - a restart while the port is occupied keeps retrying without reporting healthy.

### 2. Gateway forwards provider-hosted tools and file parts verbatim (app/gateway/server.py:349-356)
The key-level allowlist is right, but values are passed through unchecked. Two value-level doors
reach provider-side billed features:
- `tools[*].type` other than `"function"`. Groq serves built-in `browser_search` /
  `code_interpreter` tools on allowlistable free-plan models, and these are billed per use outside
  the Free plan. Other provider hosted-tool types are in the same class.
- `messages[*].content[*].type == "file"` (OpenRouter). When the model has no native file support,
  OpenRouter PDF handling defaults to a paid OCR engine, even for `:free` models.

The OpenCode openai-compatible provider only ever sends `type: "function"` tools, so a strict check
breaks nothing.

Fix:
- After the key filter, return 400 `unsupported_tool_type` if `tools` is not a list of dicts with
  `type == "function"`.
- Return 400 `unsupported_content_type` if any message content part has `type == "file"`
  (allow `text` and `image_url`).
- Neither case makes an upstream call or writes a usage row.
- Tests:
  - `browser_search` gives 400 with 0 upstream calls;
  - function tools still pass;
  - a file part gives 400.
- Add a SPEC section 6.6 sentence: "the gateway also refuses provider-hosted tools and file parts".

## SHOULD FIX (non-blocking, this round if cheap)
- **S1. R1 wording (SPEC.md:448, ARCHITECTURE.md:726, docs :148).** The text is accurate but
  narrower than the real exposure. "Any credential readable by that OS user" also includes ambient
  cloud credentials (`~/.aws/credentials`, gcloud ADC, Azure CLI, Copilot tokens). OpenCode can find
  those through the allowlisted `HOME`/`XDG_*` (only `enabled_providers` stops OpenCode itself from
  using them), and the agent shell and project plugins can use them directly. Add one line, plus a
  setup checklist row: the box must hold no such credentials until T072.
- **S2. A misleading test name.** `tests/test_opencode_hardening.py:98`
  `test_stored_credentials_added_later_block_restart_path_not_leak` only covers an explicit
  `start()`. The monitor restart path (`server.py:281-294`) does not re-run `auth_store_problem`.
  The per-task check 0 covers it, so either rename the test or add the check in `_launch`.
- **S3. `--once` now needs a working opencode binary** (changes.md deviation). This is acceptable, but
  `--once` help text (`app/cli.py:44`) should say so, or `once=True` should skip the manager.

## Verified OK (bypass hunt)
- **Env leakage.** Allowlist only. Passthrough rejects `OPENCODE_*`/`AWS_*`/`AZURE_*`/`*_API_KEY`,
  `GOOGLE_APPLICATION_CREDENTIALS`, provider `api_key_env` and the bot token env. Both children are
  tested, including the parent `OPENCODE_CONFIG_CONTENT`.
- **auth.json.** Checked at start and before every task, off-loop. Values are never read out.
- **Generated config and substitution.** All interpolated strings are validated or generated. There
  is no `{`/`}` path.
- **run --attach child.** Gets the allowlisted env plus the server password only. `--model broke/...`.
- **Restart path.** Regenerates the same gateway-only config. The gateway is still up during
  restarts, because it is stopped after the manager.
- **Gateway model handling.** Exact dict lookup. Non-str, case, whitespace, NUL, unicode and `*`
  variants give 403 (tested).
- **OpenRouter routing fields, redirects, URL.** Dropped, never followed, fixed.
- **Streaming.** Passthrough only after a 2xx. The usage parser is bounded at 1 MiB.
- **Auth.** 32-byte urlsafe token, constant-time compare, checked before the body. 24-byte serve
  password. Both are added to the redactor.
- **Bind.** Gateway `127.0.0.1` (real-socket test). Serve gets `--hostname 127.0.0.1`.
- **Redaction.** Upstream errors go through the adapter redaction, then the redactor and the token
  scrub. Transport errors expose the class name only. Stderr tails are redacted.
- **Lifecycle.** Health wait with timeout, early exit gives the exit code and stderr detail,
  exponential backoff capped and reset after 60 s uptime, POSIX killpg / Windows taskkill /T tree
  kill (grandchild test), idempotent stop.
- **Daemon with no Discord token.** NullFrontend; starts cleanly (smoke run above).

## Follow-ups (carried forward, current status)
1. **F1** (prior required fixes 1-4):
   - Fix 1 (markdown agents): SUPERSEDED. `enabled_providers: ["broke"]` plus the gateway allowlist.
   - Fix 2 (scan to root): DONE.
   - Fix 3 (server-cwd masking): SUPERSEDED. The daemon owns the serve cwd and the inline config.
   - Fix 4: `command` and project `provider` are DONE. Alias/baseURL: SUPERSEDED by the gateway.
2. **F2, F3, F4: RESOLVED.** The F3 residual (keys passed to `--attach`) is RESOLVED by the env
   allowlist.
3. **F5 (open, rare):** `logger.exception` in daemon.py logs full tracebacks.
4. **F6 (open):** output_tail truncation can split a secret, so it escapes redaction.
5. **F7: RESOLVED** (`trust_env=False` on health, abort and /config).
6. **F8 (open):** RunningTask session_id is an empty string. Use None.
7. **F9 (open):** any authorized user can cancel or continue any task.
8. **F10: RESOLVED** (http server_url only for localhost).
9. **F11: SUPERSEDED** (provider_map removed).
10. **F12-F16 (open, low):** QUEUED->ROUTING DB error; kill grace vs shutdown timeout; finally for
    non-cancel exceptions after spawn; monotonic OK-cache clock; command[0] allowlist.
11. **F17: RESOLVED** (`tests/test_wiring.py:104`).
12. **F18: RESOLVED**, including the `enabled_providers` exact check.
13. **F19: RESOLVED** (R1-R4 documented). See S1 for the wording.
14. **F20, F21, F22: SUPERSEDED** (managed serve; no keys in OpenCode).
15. **F23:** folded into T082 (G1-G8). Add to T082:
    - confirm whether GET /config echoes `provider.broke.options.apiKey`; if it does, compare it to
      the gateway token as an identity check;
    - confirm the merge order of `enabled_providers` (array replace, not concat).
16. **F24: SUPERSEDED** for keys (serve and plugins are key-free). The plugin code-execution class
    remains under R1/T072.
17. **Known gap (open):** a session cancelled before its first event is not aborted server-side.
18. **Deferred (still open):**
    - approvals T060/T061 and /approve, /deny, /attach;
    - start scripts T101/T102;
    - live E2E T082;
    - B4 internal 429 visibility;
    - quota refresh T044;
    - concurrent tasks.

    Server Basic auth is now DONE (T071).
19. **Slice-2 follow-ups (still open):**
    - snapshot max-age;
    - 504 precedence;
    - ratelimit header clamps;
    - partial-key and other-scheme redaction;
    - install_redaction for late handlers;
    - A9 Groq header scoping;
    - probe-cost reuse;
    - test_cli redaction-filter hygiene;
    - slice-1 test strengthening.
20. **Strict xfail (open):** delete it if it is the paid-spend one. It is moot under free-only.
21. **N1 (new, low):** the OpenCode bash tool may spawn commands in their own process groups
    (detached). In that case the serve killpg will not reap them on stop or restart. Verify at T082.
    A cgroup/systemd scope arrives with T072.
22. **N2 (new, low):** broaden the `env_passthrough` rejects to `*_TOKEN`, `*_SECRET`, `*_KEY`,
    `*_PASSWORD`.
23. **N3 (new, low):** `_relay_stream` returns an unprepared StreamResponse if `prepare()` itself
    raises (app/gateway/server.py:476). Return a 502 JSON error in that case.
24. **N4 (new, low):** the agent holds the serve password and can `PATCH /config` or `PUT /auth/*` on
    serve. That is harmless without keys, but it belongs on the R1/T072 list.
25. **R1 (accepted, next round T072):** the agent can read secrets.env and /proc/<daemon>/environ
    (plus the S1 ambient credentials). It is documented and not blocking.

**Dropped earlier as moot because of the free-only change:** the paid-spend, budget and paid-approval
items.
