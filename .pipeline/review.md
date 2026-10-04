# Re-review: F1 completion round (fixes A, B, F18), uncommitted on top of 8b79542

VERDICT: NEEDS WORK

Gate, re-run by me with `.venv/Scripts/python`:
- `pytest -q`: 929 passed, 1 xfailed.
- `ruff check .`: clean.
- `ruff format --check .`: 119 files formatted.
- `-m app --check --config config.example.yaml`: OK.

Summary:
- Fix A (allowlist scope) is correct and complete.
- F18 (agent/mode model keys in GET /config) is correct.
- Fix B (on-disk project check) is well built and fails closed for the files it looks at. But it
  misses config locations that OpenCode loads for a project, and I reproduced four of them against the
  real checker: they return OK while a paid model is configured. Three are realistic without any
  malicious actor.
- The GET /config check can also be masked by the server start directory's own project config.
- F1 therefore still does not deliver "OpenCode cannot select a non-allowlisted model via config".
  It is close; the fixes below are small and local.

Reproduction: a scratchpad script calls `check_project_config(project, {"groq/free-a"})`, with
PAID = `openrouter/anthropic/claude-opus-4`.

| # | Planted file | Result |
|---|---|---|
| 1 | no `.git`; parent `ws/opencode.json` sets `{"small_model": PAID}` | OK (miss) |
| 2 | `.opencode/agent/x.md` with frontmatter `model: PAID` | OK (miss) |
| 3 | `.opencode/agents/build.md` with frontmatter `model: PAID` | OK (miss) |
| 5 | `opencode.json` sets `{"command": {"c": {"model": PAID}}}` | OK (miss) |
| 6 | `opencode.json` sets `{"provider": {"groq": {"models": {"free-a": {"id": "paid-x"}}}}}` | OK (miss) |
| 7 | `opencode.json` sets `provider.groq.options.baseURL` | OK (miss) |
| 11 | control: `opencode.json` sets `{"small_model": PAID}` | UNSAFE (correct) |

## REQUIRED FIXES

### 1. Markdown agents and modes are not checked (config_check.py `_candidate_files`)
- What OpenCode loads: agents defined in markdown at `.opencode/agent/*.md` (newer versions also
  `.opencode/agents/`). Their YAML frontmatter supports `model: provider/model`. Modes work the same
  way under `.opencode/mode(s)/*.md`, and commands under `.opencode/command(s)/*.md`. All are loaded
  from every `.opencode` directory in the project scan and merged into config.agent / config.mode /
  config.command.
- Paid path: the task tool starts a subagent with `agent.model` when it is set; the `--model` given to
  the parent run does not apply to it. A project markdown file that overrides `build`, or adds a
  subagent whose description invites use, sends calls to the paid model.
- Why it is realistic: repos used with OpenCode plus Claude commonly ship these files. Probes 2 and 3
  return OK.
- Fix:
  - For each scanned dir, read `*.md` in `.opencode/{agent,agents,mode,modes,command,commands}`.
  - Parse the leading `---` frontmatter block.
  - If any `model` key is present, it must be an allowlisted string. Treat an unparseable frontmatter
    that contains a model line as UNSAFE.
  - A conservative line match is acceptable: `^\s*model\s*:` inside the frontmatter, value
    unquoted/stripped, must be in the allowed set, otherwise UNSAFE.
- Tests: paid agent/ and agents/ markdown files are UNSAFE; with an allowlisted model they are OK;
  with no model key they are OK.

### 2. Projects without git: OpenCode searches up to the filesystem root, the checker only checks the project dir (config_check.py `_scope_dirs`; tests/test_project_config_check.py:165)
- For a directory outside git, OpenCode's project worktree is "/". Config discovery (findUp of
  `opencode.json(c)` and `.opencode`) walks from the directory up to that worktree, which here means
  the filesystem root.
- Example: `/workspace/myproj` without `.git`, where `/workspace/opencode.json` sets a paid model.
  The checker says OK (probe 1).
- Projects under /workspace are a documented layout. The agent of any task can write
  `/workspace/opencode.json`, and that file then applies to every non-git sibling project.
- `test_without_git_only_project_dir_checked` asserts the unsafe behaviour and must be inverted.
- Fix: when no `.git` is found, return the full chain up to the root. Simpler and also robust to
  git-worktree/common-dir edge cases: always scan to the root. The cost is a possible false UNSAFE
  from a stray file above the git root, which fails closed. Update B13, D2b and the example comment.

### 3. The GET /config check can be masked by the server's start-directory project config (B12)
- Precedence:
  - GET /config returns the merged config of the server's start directory instance.
  - Project files there override global and `OPENCODE_CONFIG` (only `OPENCODE_CONFIG_CONTENT` ranks
    higher).
  - So keys set in the server-cwd project hide the global values.
- Concrete path:
  - `opencode serve` is started from /workspace/a, whose `opencode.json` sets `small_model: groq/<free>`.
  - Global sets no `small_model`.
  - GET /config returns OK.
  - A task in /workspace/b, which has no project config, passes the on-disk check.
  - Instance b has no `small_model`, so OpenCode auto-picks a "cheap" model from the provider. For
    OpenRouter that is plausibly a paid Haiku-class model. This is exactly the case B12 exists to
    prevent.
  - D4 does not say which directory to start `serve` from.
- Fix (either option):
  - (a) Also check the global files on disk from the bot, which runs as the same WSL user:
    - `$XDG_CONFIG_HOME` or `~/.config/opencode/{opencode.json,opencode.jsonc,config.json}`, plus its
      agent(s)/, mode(s)/ and command(s)/ markdown;
    - same validator;
    - require `small_model` to be present and allowlisted in the global file OR in the task's project
      scope.
  - (b) Use `GET /path` (directory/worktree) to learn the server's directory, and require that it
    carries no project config (no files from `_candidate_files`).
- Either way, D4 must say to start `opencode serve` from $HOME, which has no project config, and
  never from a project.

### 4. Model-redirecting keys not covered by `_bad_model_key` (project files and GET /config)
- `command.<n>.model` (probe 5).
- `provider.<id>.models.<m>.id` (probe 6). OpenCode uses this as the API model id, so the
  allowlisted name `groq/free-a` can be aliased to a different upstream model. Re-confirm the field
  at T082; the fix is to fail closed regardless.
- `provider.<id>.options.baseURL` (probe 7). This redirects requests, and the provider API key, to
  any endpoint: key exfiltration and spend outside the allowlist.
- Fix:
  - Walk `command` like agent/mode.
  - In project-scope files, treat any `provider` section as UNSAFE. Projects have no legitimate need
    for one here.
  - In GET /config, treat any `provider.*.models.*.id` that differs from its key, and any
    `provider.*.options.baseURL`, as UNSAFE.
- Tests for each.

## Verification of this round

### Fix A (allowlist scope): RESOLVED
- `AppConfig` now requires an explicit `provider_map` entry mapping to openrouter/google/groq. The
  previous `.get(p, p)` identity fallback is gone, which also closes F11's "unmapped" half.
- `allowed_small_models` also requires the policy enabled, the provider present and enabled, and a
  mapping in `_FREE_PROVIDERS`. That is defence in depth, mutation-tested through the post-load
  mutation test.
- The anthropic repro is now rejected at load.

### F18 (GET /config keys): RESOLVED, with required fix 4 extending it
- `_bad_model_key` checks `model`, `small_model`, `agent.*.model` and `mode.*.model`, failing closed
  on non-dict sections, non-dict entries and non-string values.
- `small_model` is still mandatory in GET /config.
- Mutation-killed by `test_config_response_other_model_keys_unsafe`.
- Not done: `enabled_providers` subset check (suggested last round). It is still recommended.

### Fix B (project on-disk check): PARTIAL (required fixes 1-3)
What is correct:
- Runs every task, uncached, after the GET /config check and before refresh/route/spawn.
- Runs off the event loop (`to_thread`).
- An exception becomes UNSAFE.
- UNSAFE becomes FAILED OPENCODE_UNSAFE_CONFIG, with no attempt row, cooldown or spawn (tested).
- The detail carries the path and key path only, never the value (tested), and goes through
  `safe_text`/redactor.
- Parse failure and non-object files are UNSAFE.
- A per-project result cannot vouch for another project (tested).

JSONC review of `strip_jsonc`:
- String scanning with escapes is right. URLs inside strings survive (tested).
- Comment removal and trailing-comma removal across comments are correct.
- An unterminated comment drops the rest of the file. OpenCode's jsonc-parser errors on that file,
  so it is not a bypass.
- Duplicate keys: last wins in both parsers.
- Python-only extensions (NaN) are harmless.
- UTF-16 or bad UTF-8 raises a ValueError subclass, which makes the file UNSAFE.
- I found no differential where OpenCode would see a model key the checker does not, apart from
  {env:VAR} substitution. OpenCode substitutes raw text before parsing, so a server env value can
  inject a key inside a string field. That needs the server's env to be hostile, so it stays low;
  see F24.

Stripping OPENCODE_CONFIG/OPENCODE_CONFIG_CONTENT in `_child_env`:
- Correct and tested.
- It is largely cosmetic for --attach, because config is resolved by the server process, not the
  client. The server's own env is what matters.
- OPENCODE_CONFIG_CONTENT on the server has the highest precedence, so it always appears in
  GET /config and is covered.
- OPENCODE_CONFIG, OPENCODE_CONFIG_DIR and global files on the server are covered only when not
  masked by the server-cwd project (required fix 3).
- Docs already say to start serve without these variables.

### Tests
Meaningful: the real checker runs over tmp trees, the real executor runs over MockTransport, the
orchestrator is asserted to never spawn, and there are mutation kills. Two problems:
- One test pins wrong behaviour (`test_without_git_only_project_dir_checked`).
- Markdown, command, provider and global-file cases are absent.

## Residual risks (document in spec section 11 / B13; not code blockers)
- **R-a: the agent's own tools.** The OpenCode agent has bash and runs inside the server process,
  whose env holds the provider keys. It can:
  - curl a paid endpoint;
  - run opencode run -m with a paid model;
  - PATCH /config on the unauthenticated localhost server;
  - write a plugin.
  No config check can prevent this. The only hard guarantee is account-level: a $0 OpenRouter balance,
  no billing on the Google AI Studio project, and the Groq Free plan. Docs Part B already require
  that. State it in the spec as THE free-only guarantee, with the config checks as accident
  prevention.
- **R-b: TOCTOU and cached config.**
  - The server caches each directory instance's config. A mid-task write normally takes effect only
    when the instance reloads, and the next task's on-disk check catches it (tested between tasks).
  - The reverse case: an instance created earlier (for example by a TUI session) while a paid config
    was on disk keeps that stale config after the file is fixed.
  - Document "restart opencode serve after any config change". Confirm at T082 whether OpenCode
    hot-reloads config on file change.
- **R-c: symlinks.**
  - `_scope_dirs` uses resolve(), while OpenCode walks the unresolved cwd path.
  - A symlinked project dir can therefore have different parents.
  - Owner-configured, so low. Scanning both the resolved and unresolved chains closes it.
- **R-d: case-insensitive filesystems.** Under .opencode/, iterdir matches the suffix
  case-sensitively. On Windows/NTFS-backed paths, .opencode/OPENCODE.JSON would be loaded by OpenCode
  but skipped by the checker. On the documented WSL ext4 layout it is not loaded, so this is low.
  Lower-case the suffix, or add the explicit names.
- **R-e: plugins** (plugin key, .opencode/plugin(s)/) run arbitrary code in the server. This is the
  same class as R-a; see F24.

## Follow-ups (ranked, current status)
1. **F1: OPEN** (required fixes 1-4 above). Parts A and F18 are resolved.
2. **F2: RESOLVED.**
3. **F3: RESOLVED.** Residual: provider key env vars are still passed to the --attach client. Drop
   them after T082.
4. **F4: RESOLVED.**
5. **F5 (open, rare):** logger.exception in daemon.py logs full tracebacks. Switch to class-only.
6. **F6 (open):** output_tail truncation can split a secret, so it escapes redaction.
7. **F7 (open):** trust_env=False on the health, abort and /config httpx clients.
8. **F8 (open):** RunningTask session_id is an empty string. Use None.
9. **F9 (open):** any authorized user can cancel or continue any task.
10. **F10 (open):** require localhost for an http server_url. Both /config checks now trust the
    server too.
11. **F11: PARTIAL.** Unmapped policy providers are now rejected (fix A). Still open: pin provider_map
    to the canonical pairs.
12. **F12 (open, low):** QUEUED -> ROUTING DB error leaves the row QUEUED.
13. **F13 (open, low):** kill grace plus abort timeout can exceed shutdown_timeout_s.
14. **F14 (open, low):** a non-cancel exception after spawn skips kill and abort. Add a finally.
15. **F15 (open, low):** monotonic clock for the small_model OK cache.
16. **F16 (open, low):** allowlist opencode.command[0] instead of a shell denylist.
17. **F17: RESOLVED in substance** (wiring passes bot_token_env), but still no test_wiring assertion
    pins the kwarg. Add one.
18. **F18: RESOLVED** for agent/mode. Extended by required fix 4 (command, provider). Still open:
    enabled_providers subset check in GET /config, and recommend setting it in D2b.
19. **F19 (new):** spec section 11 / B13 must state R-a: account-level $0/no-billing is the actual
    free-only guarantee.
20. **F20 (new):** docs D4: start opencode serve from HOME, without OPENCODE_CONFIG* env, and restart
    it after any config change (R-b, required fix 3).
21. **F21 (new, low):** symlink chains (R-c).
22. **F22 (new, low):** case-insensitive .opencode file matching (R-d).
23. **F23 (new, low):** confirm at T082:
    - the provider.*.models.*.id alias field;
    - markdown agent dirs (agent/ vs agents/);
    - instance config caching and hot-reload;
    - non-git worktree "/".
24. **F24 (new, low):** project files containing {env:/{file: substitution, a plugin key, or a
    .opencode/plugin(s)/ dir: consider failing closed.
25. **Known gap (informational):** a session cancelled before its first event is not aborted
    server-side.
26. **Deferred per spec section 13 (still open):**
    - approvals T060/T061 and /approve, /deny, /attach;
    - start scripts T101/T102;
    - live E2E T082;
    - server Basic auth;
    - B4 internal 429 visibility;
    - quota refresh scheduler T044;
    - concurrent tasks.
27. **Slice-2 follow-ups (still open):**
    - snapshot max-age;
    - 504 precedence;
    - ratelimit header clamps;
    - partial-key and other-scheme redaction;
    - install_redaction for late handlers;
    - A9 Groq header scoping;
    - probe-cost reuse;
    - test_cli redaction-filter hygiene;
    - slice-1 test strengthening.
28. **Remaining strict xfail:** delete it if it is the paid-spend one. It is moot under free-only.

**Resolved:** R1, R2, R3, R4, F2, F3, F4, F1-A, F18 (agent/mode).

**Dropped earlier as moot because of the free-only change:**
- paid-spend fail-closed and NaN budget checks;
- the paid_spend missing-cost guard;
- the paid approval flow;
- PAID_BUDGET_EXHAUSTED and budget windows;
- the estimated_cost_usd validation items.
