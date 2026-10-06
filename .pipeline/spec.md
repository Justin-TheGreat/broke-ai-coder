# Spec: go-live readiness round (branch feat/go-live, on 331ae69)

## OPEN QUESTIONS

None of these block the work. The decisions below were made by the planner. Flag any of them in changes.md if the code shows it is wrong.

- D1. **Defaults cannot be removed.** The built-in deny list cannot be removed or overridden; config can only add patterns. SPEC §5.2's "unless explicitly disabled for a trusted local project" stays future work, together with T061.
- D2. **Permission keys.**
  - `doom_loop` is also set to `"deny"`. OpenCode's default for it is `"ask"`, and "ask" is undocumented in headless mode.
  - `webfetch`, `question`, `edit` and `read` are left at OpenCode's defaults (not requested).
- D3. **websearch is denied.** It sends queries to a third-party search service (Exa) outside the gateway. Its cost and billing terms are not verified, and the free-only rule says that unverified means deny. It is probably not even registered without `OPENCODE_ENABLE_EXA`, so this is belt and braces.
- D4. **T102 uses a system unit.** The unit is `scripts/systemd/broke-ai-coder.service` with `User=`, not a user unit, because:
  - it starts at distro boot without `loginctl enable-linger`;
  - it does not depend on the WSL user-session/dbus manager;
  - it logs to the system journal.
  It is an example only: the user installs it with sudo.
- D5. **No repository Python code runs as root.** `wsl-setup.sh` runs every Python helper as the daemon user through `runuser -u "$SUDO_USER" --` and does only file and user operations as root.

---

## 1. T060 minimal: OpenCode permission deny list

### 1.1 New file `app/opencode/permissions.py`

Module docstring: the deny list is a **guardrail, not a security boundary**. Bypasses exist: wrapper scripts (`bash x.sh`), interpreters, plugins, project agent markdown, and `git -C dir push`. The boundary is the OS user (no credentials, no sudo, no secrets; T072). Deny is stricter than approval: T061 approval buttons are future work. List the assumptions P1-P6 (section 1.6) in the docstring, in the same style as `app/opencode/os_user.py`.

```python
DENY = "deny"
ALLOW = "allow"
DENIED_TOOLS: tuple[str, ...] = ("external_directory", "websearch", "doom_loop")
DEFAULT_BASH_DENY: tuple[str, ...] = (...)  # exact list below, in this order

def bash_deny_patterns(cfg: OpenCodePermissionConfig) -> tuple[str, ...]:
    """DEFAULT_BASH_DENY followed by cfg.extra_bash_deny; duplicates dropped, first occurrence kept."""

def build_permission_block(cfg: OpenCodePermissionConfig) -> dict[str, Any]:
    """{"bash": {"*": "allow", <p>: "deny", ...}, "external_directory": "deny",
        "websearch": "deny", "doom_loop": "deny"}. "*" is the FIRST bash key (last match wins)."""

def default_permission_block() -> dict[str, Any]:
    return build_permission_block(OpenCodePermissionConfig())

def permission_problem(body: Mapping[str, Any], required: Mapping[str, Any]) -> str | None:
    """Key path of the first problem in a merged OpenCode config, or None. Never includes pattern text."""

def agent_permission_problem(section: str, entries: Any) -> str | None:
    """Shared with the project-file check (see 1.4)."""
```

`DEFAULT_BASH_DENY`, exactly these strings in this order. Comments group them by category: publication/push, bulk delete, deploy/production, environment-changing installs, credentials. None may contain `API_KEY` or `sk-`; `tests/test_opencode_server.py:115` asserts that.

```
"*git push*", "gh auth*", "gh repo delete*", "gh release*", "docker push*", "npm publish*",
"pnpm publish*", "yarn publish*", "yarn npm publish*", "cargo publish*", "twine upload*",
"gem push*", "poetry publish*", "uv publish*",
"rm -r*", "rm -R*", "rm -fr*", "rm -fR*", "rm -f -r*", "rm --recursive*", "rm * -r*",
"rm * -R*", "rm * -fr*", "rm * -fR*", "rm * --recursive*", "find * -delete*", "git clean*",
"git rm -r*", "shred *",
"kubectl *", "helm *", "terraform apply*", "terraform destroy*", "pulumi up*",
"pulumi destroy*", "vercel*", "netlify deploy*", "fly deploy*", "flyctl deploy*",
"firebase deploy*", "heroku *", "serverless deploy*", "sls deploy*", "gcloud *", "aws *",
"az *", "ansible-playbook*", "ssh *", "scp *",
"sudo*", "su *", "apt *", "apt-get *", "aptitude *", "dpkg -i*", "snap *", "brew *",
"npm install -g*", "npm install --global*", "npm i -g*", "npm i --global*", "npm -g *",
"pnpm add -g*", "pnpm add --global*", "pnpm install -g*", "yarn global*",
"pip install --user*", "pip3 install --user*", "python -m pip install --user*",
"python3 -m pip install --user*", "*--break-system-packages*", "pipx *", "uv tool install*",
"cargo install*", "go install*", "gem install*",
"git config --global*", "git config --system*", "git credential*", "opencode auth*",
"ssh-keygen*", "ssh-add*", "ssh-copy-id*", "npm login*", "npm adduser*", "npm token*",
"docker login*", "passwd*", "chpasswd*"
```

`permission_problem(body, required)` rules. Return the first matching key path:

1. `body["permission"]` is missing or not a dict → `"permission"`.
2. For each `k` in `DENIED_TOOLS` that is present in `required`: if `perm.get(k) != "deny"` → `f"permission.{k}"`.
3. `bash = perm.get("bash")` is not a dict → `"permission.bash"`.
4. Walk `bash.items()` in order. Any value that is not the string `"deny"` → `"permission.bash"`, except index 0 with key `"*"`, which may hold any string. This is what makes last-match-wins safe: every rule after the leading `"*"` is a deny.
5. For each pattern `p` in `required["bash"]` other than `"*"`: if `bash.get(p) != "deny"` → `"permission.bash"`.
6. For `section` in `("agent", "mode")`, return `agent_permission_problem(section, body.get(section))` if it is not None.

`agent_permission_problem(section, entries)`:
- If `entries` is not a dict, return None. The model check already reports a non-dict section.
- For each `name, entry` whose `entry` is a dict containing `"permission"`, with `p = entry["permission"]`:
  - `p` is not a dict → `f"{section}.{name}.permission"`.
  - For `k` in `DENIED_TOOLS`: if `k in p and p[k] != "deny"` → `f"{section}.{name}.permission.{k}"`.
  - If `"bash"` is in `p`:
    - `p["bash"] == "deny"` is OK;
    - a dict whose values are all `"deny"` is OK;
    - anything else → `f"{section}.{name}.permission.bash"`.
  - Other keys (edit, read, ...) are not checked.

### 1.2 Config: `app/config/models.py`

```python
class OpenCodePermissionConfig(BaseModel):
    model_config = _CFG
    extra_bash_deny: list[str] = Field(default_factory=list)
```

Add a `field_validator("extra_bash_deny")`, following the style of `PolicyConfig._models_valid`. Strip each entry, then raise `ValueError` if any of these is true:
- the entry is empty;
- it is longer than 200 characters;
- it contains a control character (`ord < 0x20` or `0x7f`);
- it contains `{` or `}` (OpenCode `{env:`/`{file:` substitution, G8);
- it equals `"*"` (it would collide with the leading allow key).

Duplicates are allowed; they are de-duplicated later. `extra="forbid"` already rejects any attempt to set a different key such as `bash_deny`.

Add `permission: OpenCodePermissionConfig = Field(default_factory=OpenCodePermissionConfig)` to `OpenCodeConfig`. Do not import `app.opencode` from models; that would create a cycle.

### 1.3 `app/opencode/server.py`

- `build_opencode_config` adds `"permission": build_permission_block(config.opencode.permission)` to the returned dict. Put it after `"small_model"`.
- In `_spawn`, log once per launch at INFO: `"opencode permissions: %d bash patterns denied (%d from config); external_directory, websearch, doom_loop denied"`.
- Update the docstring bullet list to mention the permission block.

### 1.4 Preflight: `app/opencode/config_check.py`

- `check_server_config(..., required_permission: Mapping[str, Any] | None = None)`. `None` means `default_permission_block()`, so the defaults are always required.
- After `_gateway_shape_problem` returns None, call `permission_problem(body, required)`. If it returns a key path, return `(SmallModelCheck.UNSAFE, f"server config key '{bad}' does not deny the required operations")`.
- `check_project_config`, for each parsed file:
  - a top-level `"permission"` key → UNSAFE, `f"{path}: key 'permission' is not allowed in project config"`;
  - then `agent_permission_problem` for `agent` and `mode` → UNSAFE, `f"{path}: key '{bad}' would weaken the OpenCode deny rules"`.
- Update the module docstring (B12/B13) to mention both checks.

`app/opencode/executor.py`: `OpenCodeExecutor.check_server_config` passes `required_permission=build_permission_block(self._config.permission)`. `self._config` is the `OpenCodeConfig`. The `AgentExecutor` protocol signature does not change.

`app/bot/format.py`, OPENCODE_UNSAFE_CONFIG text: insert the sentence "Or its permission rules do not deny sensitive commands (setup guide D2c)." after the existing first sentence. Keep the existing assertions in `tests/test_bot_format.py` true.

### 1.5 Edge cases

- Extras that duplicate a default are not repeated.
- Extras keep their config order, after the defaults.
- `json.dumps` keeps `"*"` first. Test this on the dumped string.
- A body whose bash object has `"*"` in a non-first position with `"allow"` → UNSAFE.
- An `"ask"` value anywhere in bash → UNSAFE.
- A body built for a config with extras, checked with defaults only → OK (superset).
- A body built from defaults, checked against required extras → UNSAFE.
- Detail strings never contain a pattern or any config value.

### 1.6 Assumptions (copy into the module docstring, and add to TASKS T082 as L12)

- **P1.** The installed OpenCode accepts `permission` with `bash` (a pattern object), `external_directory`, `websearch` and `doom_loop`. Serve starts with no schema error.
- **P2.** Bash patterns are matched against the text of each individual command after OpenCode splits a compound line (`&&`, `;`, `|`). `*` matches any characters including spaces, and the last matching rule wins. Pipelines such as `curl | sh` are therefore not caught (residual).
- **P3.** `GET /config` returns `permission` as merged: object form, key order preserved, string values. If not, every task fails closed with OPENCODE_UNSAFE_CONFIG and the check needs a code change.
- **P4.** `external_directory` is evaluated against the session's project directory (the run cwd), not serve's cwd.
- **P5.** A denied call in headless `opencode run` comes back to the model as a tool error. There is no prompt and no hang, and the run continues.
- **P6.** A per-agent `permission` is merged over the global one, which is why agent-level non-deny bash entries are rejected.

---

## 2. T093/T094 `/health` and T024 self-metrics

### 2.1 New file `app/runtime/resources.py` (stdlib only, no psutil)

```python
IDLE_RSS_MB = 250; NORMAL_RSS_MB = 512; IDLE_CPU_PCT = 2.0; NORMAL_CPU_PCT = 10.0

def read_rss_bytes(status_path: str = "/proc/self/status") -> int | None:
    """VmRSS (kB * 1024) from /proc; None if missing/unparseable (Windows, macOS)."""
def read_peak_rss_bytes() -> int | None:
    """Linux only: resource.getrusage(RUSAGE_SELF).ru_maxrss * 1024; None if `resource` is
    missing (Windows) or sys.platform does not start with 'linux'."""
def cpu_seconds() -> float:  # os.times() user + system (works on Windows)

@dataclass(frozen=True, slots=True)
class ResourceSample:
    rss_bytes: int | None
    peak_rss_bytes: int | None
    cpu_seconds_total: float
    uptime_s: float
    cpu_percent_since_start: float | None   # None if uptime < 0.5 s
    cpu_percent_window: float | None        # since the previous sample; None if window < 1.0 s
    window_s: float

class ProcessSampler:
    def __init__(self, *, clock=time.monotonic, cpu=cpu_seconds, rss=read_rss_bytes,
                 peak=read_peak_rss_bytes) -> None   # records start (clock(), cpu())
    def sample(self) -> ResourceSample              # updates the "previous" point
```

- CPU percent means percent of one core: `100 * dcpu / dwall`.
- No reader may raise. Exceptions are caught, and the reader returns None (or 0.0 for cpu).

### 2.2 Probes: new file `app/runtime/diagnostics.py`

```python
@dataclass(slots=True)
class RuntimeProbes:
    sampler: ProcessSampler = field(default_factory=ProcessSampler)
    gateway_up: Callable[[], bool] | None = None
    serve_state: Callable[[], tuple[bool, str | None]] | None = None   # (ready, fatal_error)
    os_user_summary: Callable[[], str] | None = None
```

Supporting changes:
- `app/gateway/server.py`: add `def is_running(self) -> bool: return self._runner is not None`.
- `app/opencode/os_user.py`: add `def status_summary(self) -> str` to the `OpenCodeLauncher` Protocol, `DirectLauncher` and `SudoLauncher`. Neither may raise.
  - `DirectLauncher`: `"mode none: OpenCode runs as the daemon user (tests/dev only, unsupported for production)"`.
  - `SudoLauncher`:
    - Keep `self._checked_at: datetime | None`, set with `app.timeutil.utcnow()` in `start()` after `self_check()` passes.
    - Before that: `f"sudo as {name}: self-check not run yet"`.
    - After: `f"sudo as {name}: isolation self-check passed at {checked:%Y-%m-%d %H:%M} UTC; WSL interop now {state}"`. `state` is `"disabled"`, `"ENABLED (unsafe; restart required)"` from a live `self._probe.interop_enabled()`, or `"unknown"` if that raises.
- `app/runtime/wiring.py`:
  - Build `RuntimeProbes(gateway_up=gateway.is_running, serve_state=(lambda: (manager.ready, manager.fatal_error)) if manager else None, os_user_summary=launcher.status_summary)`.
  - Add a `probes: RuntimeProbes` field to `Runtime` (last field).
  - Pass it as `create_frontend(config, redactor=..., environ=..., probes=probes)`.
- `app/bot/discord_frontend.py`:
  - `create_frontend` and `DiscordFrontend.__init__` take `probes: RuntimeProbes | None = None`.
  - `start` builds `ControllerBackend(controller, self._config, probes=self._probes)`.
  - Register `@tree.command(name="health", description="Controller diagnostics (daemon only; excludes OpenCode)")`, which calls `self._handle(interaction, "health", {})`.

### 2.3 Commands and format

`app/bot/commands.py`:
- `COMMAND_NAMES` gains `"health"`. `tests/test_discord_frontend.py:75` then forces the registration.
- Add this dataclass:

```python
@dataclass(frozen=True, slots=True)
class HealthSnapshot:
    resources: ResourceSample | None
    sqlite_db_bytes: int | None
    last_cleanup_at: str | None
    task_queue_depth: int
    event_queue_depth: int
    event_dropped_count: int
    running_task_id: str | None
    provider_cooldowns: int   # active entries with model None
    model_cooldowns: int      # active entries with a model
    gateway_up: bool | None   # None = unknown/not wired
    serve_ready: bool | None  # None = not managed
    serve_fatal_error: str | None
    os_user: str | None
```

- `BotBackend` gains `def health(self) -> HealthSnapshot: ...`.
- `_dispatch`: `"health"` → `Reply(fmt.format_health(backend.health(), redactor=redactor), True)`.
- Authorization is the existing `authorize`, so allowed users count as admins.

`app/bot/backend.py`, `ControllerBackend.__init__(..., probes: RuntimeProbes | None = None)` and `health()`:
- Use `controller.metrics()` for the queue depths, the dropped count, `last_cleanup_at`, `sqlite_db_bytes` and `running_task_id`.
- Get the cooldown counts from `orch.cooldowns.active(utcnow())` when `orch` is not None, else 0.
- Read the remaining fields from the probes. A missing probe gives None; with no probes, `resources` is None.

`app/bot/format.py`, `format_health(snapshot, *, redactor=None) -> str`, through `safe_text`. The lines, in order:

```
Controller health (daemon process only; excludes OpenCode and project child processes)
RSS: 85.2 MB (peak 90.1 MB); targets idle < 250 MB, normal < 512 MB
CPU: 0.4% of one core over the last 120 s; 0.3% since start (12.3 s CPU, up 2h 03m); targets idle < 2%, normal < 10%
SQLite: 1.2 MB; last cleanup 2026-10-06T10:00:00Z
Queues: tasks 0, events 0 (dropped 0); running task: none
Provider cooldowns: 1 provider-wide, 2 model
Gateway: up
OpenCode serve: ready
OS user: <summary>
```

Formatting rules:
- MB means `bytes / 1_048_576` with one decimal place.
- An unknown RSS or peak prints `unavailable on this platform`. Omit the peak part when it is None.
- Omit the window part when `cpu_percent_window` is None.
- With `resources` None: `Resources: unavailable`.
- `last_cleanup_at` None → `never`.
- The running task is shown by its first 8 characters.
- Gateway: True → `up`, False → `DOWN`, None → `unknown`.
- Serve:
  - `serve_ready` None → `not managed (opencode.managed_server: false)`;
  - True → `ready`;
  - a fatal error → `FATAL (restart the daemon): <fatal[:200]>`;
  - otherwise → `down (restarting)`.
- `os_user` None → `unknown`.
- Never print an endpoint URL, a password, a token, an environment value or a path.

### 2.4 T024 benchmark: new `app/runtime/bench.py` and thin wrapper `scripts/bench_idle.py`

`scripts/bench_idle.py`:
- Inserts the repo root (the parent of `scripts/`) at `sys.path[0]`.
- Calls `raise SystemExit(main())` from `app.runtime.bench`.

`app/runtime/bench.py`:

```python
@dataclass(frozen=True, slots=True)
class BenchResult:
    duration_s: float; interval_s: float; samples: int
    rss_max_bytes: int | None; rss_last_bytes: int | None
    cpu_avg_percent: float | None; cpu_max_interval_percent: float | None

async def run_idle_benchmark(config: AppConfig, *, seconds: float, interval: float,
                             warmup: float, import_discord: bool,
                             sampler_factory: Callable[[], ProcessSampler] = ProcessSampler) -> BenchResult
def format_report(result: BenchResult) -> str
def main(argv: Sequence[str] | None = None) -> int
```

CLI options:
- `--seconds` (float, default 1800);
- `--interval` (default 5);
- `--warmup` (default 5);
- `--config` (default `config.yaml` if it exists in the cwd, else `<repo>/config.example.yaml`);
- `--no-discord-import`.

Steps:
1. Load the config with `load_config`. On ConfigError, print to stderr and return 2.
2. Override the database path to `<tempfile.TemporaryDirectory()>/bench.db` with `model_copy(update=...)` on `database`, then on the config. Never touch the user's DB. The temp dir is removed afterwards.
3. Unless `--no-discord-import`, `import app.bot.discord_frontend` so that module memory is counted. It does not connect.
4. `rt = build_runtime(config, redactor=SecretRedactor(), environ={}, once=True)`. The empty environ means no key is ever read and no Discord client runs.
5. `await rt.controller.start()`, then `await rt.gateway.start()`. Sleep `warmup`, then create the sampler.
6. Every `interval`, `sample()` until `seconds` have passed. Always take at least one final sample.
7. In `finally`: `await rt.controller.stop()`, then `await close_runtime(rt)`.

Results:
- `cpu_avg_percent` is the CPU delta over the wall delta for the whole measured window.
- `cpu_max_interval_percent` is the maximum of the `cpu_percent_window` values.

Report:
- The first line says `controller daemon only; excludes OpenCode and child processes`.
- Then one line per metric with PASS, FAIL or UNKNOWN against IDLE_RSS_MB (rss max) and IDLE_CPU_PCT (cpu avg).
- The normal targets are printed for reference.

Exit codes: 1 if any FAIL, otherwise 0; UNKNOWN does not fail. No network calls are made.

ARCHITECTURE §11.1: add a sentence saying the metrics are exposed by `/health` and measured by `scripts/bench_idle.py`. Drop nothing.

---

## 3. T100 `secrets.env.example` (repo root)

```
# Copy to ~/.config/broke-ai-coder/secrets.env, chmod 600, fill in values there only.
# Loaded only by scripts/start-daemon.sh (never from ~/.bashrc). Never commit real values.
export DISCORD_BOT_TOKEN=
export OPENROUTER_API_KEY=
export GEMINI_API_KEY=
export GROQ_API_KEY=
```

Every `export` line has an empty value. The set of names must equal the `api_key_env` values in `config.example.yaml` plus `discord.bot_token_env` (test).

---

## 4. T101 scripts (bash; LF only; executable bit)

All scripts must meet these rules:
- They start with `#!/usr/bin/env bash`, followed by `set -eu` (the two check scripts may use `set -euo pipefail`). Write them in shellcheck-clean style: quote every expansion, use `$(...)`, and use `# shellcheck source=/dev/null` before sourcing.
- They support `-h/--help` (exit 0) and reject unknown arguments (exit 2).
- They set `REPO_DIR=$(cd "$(dirname "$0")/.." && pwd -P)`, `PY="$REPO_DIR/.venv/bin/python"`, and `CONFIG` from `--config PATH`, defaulting to `$REPO_DIR/config.yaml`.
- Exit codes: 0 OK, 1 check failed, 2 usage error or refusal.
- They never print a secret value.

Add a `.gitattributes` with these rules:

```
*.sh text eol=lf
scripts/systemd/* text eol=lf
secrets.env.example text eol=lf
```

Set the executable bit in the index with `git update-index --add --chmod=+x scripts/*.sh scripts/bench_idle.py` after `git add`.

### 4.1 `scripts/start-daemon.sh`

Runs the daemon in the foreground, for tmux or systemd. Options: `--config`, plus `--secrets PATH`, defaulting to `$HOME/.config/broke-ai-coder/secrets.env`.

Refuse with exit 2 and a one-line reason in each of these cases:
- running as root (`id -u` is 0);
- `$PY` is not executable;
- the config is missing;
- the secrets file is missing, is not a regular file, is a symlink, is not owned by `id -u`, or `stat -c %a` is not `600` or `400`;
- any non-blank, non-`#` line of the secrets file fails `^(export )?[A-Za-z_][A-Za-z0-9_]*=`. Print only the line numbers, via `grep -nvE ... | cut -d: -f1`, never the content;
- the daemon is already running.

PID file: `${XDG_STATE_HOME:-$HOME/.local/state}/broke-ai-coder/daemon.pid`, with its directory created `mkdir -p` and `chmod 700`.
- "Already running" means the PID file exists, `kill -0` succeeds, and `/proc/$pid/cmdline` (with NUL bytes turned into spaces) contains `-m app`.
- A stale PID file is removed.

Then:
1. `cd "$REPO_DIR"`, `umask 077`.
2. `echo $$ > "$PIDFILE"`.
3. `set -a; . "$SECRETS"; set +a`.
4. `exec "$PY" -m app --config "$CONFIG"`. After the exec the PID is the same.

Secrets are loaded only in this process.

### 4.2 `scripts/stop-daemon.sh`

Options: `--timeout N` (default 30) and `--force`.

1. Read the PID file. If it is missing, or the process is dead: print `not running`, remove a stale file, and exit 0.
2. Verify the cmdline contains `-m app` and the process owner is `id -un`. If not, refuse with exit 2.
3. `kill -TERM`, then poll once per second until the timeout.
4. If the daemon has exited, remove the PID file and exit 0.
5. If it is still alive after the timeout:
   - with `--force`: `kill -KILL`, remove the PID file, and print `OpenCode user processes are swept at the next daemon start`;
   - without `--force`: exit 1.

Under systemd, use `sudo systemctl stop broke-ai-coder` instead, and say so in the help text.

### 4.3 `scripts/health-check.sh`

It loads no secrets. It runs and reports each step as `PASS`/`FAIL` (`set +e` around each):
- `"$PY" -m app --check --config "$CONFIG"`;
- `"$PY" -m app --check-os-user --config "$CONFIG"`;
- the daemon state from the PID file (`running (pid N)` / `not running`, informational).

Exit 1 if either check failed. Run it from `$REPO_DIR`.

### 4.4 Python helpers called by the setup scripts: new package `app/ops/` (`__init__.py`)

`app/ops/setup_values.py`, run as `python -m app.ops.setup_values --config PATH`:
- Loads the config. On ConfigError, prints the error to stderr and exits 2.
- Prints these `KEY=value` lines, in this order:
  - `OS_USER_MODE`
  - `OC_USER`
  - `OPENCODE_BIN` (command[0])
  - `SERVER_DIRECTORY`
  - `WORKING_DIRECTORY`
  - `DATABASE_PATH` (`os.path.abspath`)
  - `MANAGED_SERVER` (`true`/`false`)
- Every value must match `^[A-Za-z0-9._/+-]+$`. Otherwise print `unsafe value for KEY` to stderr and exit 2, so the scripts can parse the output with `IFS='=' read -r` and never `eval`.
- It never prints an environment value.
- Function: `def setup_values(config: AppConfig) -> list[tuple[str, str]]`.

`app/ops/wslconf.py`: `merge_wsl_conf(text: str) -> tuple[str, list[str]]` returns the new text and human-readable changes; an empty list means already compliant. It is line-based:
- comments (`#`, `;`), unknown sections and keys (e.g. `[boot] systemd=true`) are preserved;
- the output uses `\n` line endings, and CRLF input is accepted;
- section names and keys match case-insensitively, keeping the original spelling;
- whitespace around `=` is allowed.

Requirements:
- `[interop] enabled=false` and `[interop] appendWindowsPath=false`:
  - the value is compared case-insensitively, with quotes stripped;
  - a wrong value is fixed in EVERY occurrence of the key in EVERY `[interop]` section;
  - a missing key is inserted after the last key line of the first `[interop]` section;
  - a missing section is appended at the end (preceded by a blank line).
- `[automount] options`:
  - strip the surrounding quotes and split on `,`;
  - for each of `umask`, `fmask` and `dmask` present: if `int(v, 8) & 0o077 != 0o077` (or the value is unparseable), replace it with `077`;
  - if `umask` is absent, append `umask=077`;
  - write the result back as `options="..."`;
  - a missing key or section is added as above.

CLI: `python -m app.ops.wslconf check PATH` and `python -m app.ops.wslconf merge PATH`.
- `check`: exit 0 if compliant; exit 1 if not, printing the changes; exit 2 if PATH exists but is unreadable. A missing file counts as empty, so it is non-compliant.
- `merge` writes the merged text to stdout; it never writes files.
- Idempotent: `merge(merge(x)[0])` returns no changes.

### 4.5 `scripts/wsl-setup.sh` (the user runs it: `sudo scripts/wsl-setup.sh --yes`)

Arguments:
- Without `--yes`: print usage and what the script would do, then exit 2.
- Also accepts `--config`.
- Argument parsing comes before anything else.

Output: every action is printed before it runs.
- `run() { printf '[setup] RUN: %s\n' "$*"; "$@"; }`
- `ok "<what>"` prints `[setup] OK: ...`
- `fail` prints `[setup] FAIL: ...` and exits 2.

The script never reads, prints or prompts for keys or tokens, never sources the secrets file, never installs packages, and never runs repository Python as root (D5).

**Phase A: preconditions.** Check ALL of these before any change. List every failure, then exit 2.
- EUID is 0, and `SUDO_USER` is set, is not root, and exists (getent). Call this `$U`, with home `$H` from getent.
- `/proc/version` matches `microsoft` (case-insensitive).
- These commands exist: `getent groupadd useradd usermod gpasswd setfacl getfacl visudo install runuser setsid stat id mktemp cmp`. For a missing `setfacl`, print `apt install -y acl`.
- `$REPO_DIR` is owned by `$U`, is not under `/workspace`, and `$PY` is executable.
- `values=$(runuser -u "$U" -- "$PY" -m app.ops.setup_values --config "$CONFIG")` succeeds. Parse it.
- `OS_USER_MODE=sudo`.
- `OPENCODE_BIN` is a regular file, owned by root, and not group/other-writable (`stat -c '%U %a'`). Otherwise point to docs D1.
- `SERVER_DIRECTORY` and `WORKING_DIRECTORY` are absolute and not under `$H`.
- The sudoers text renders: `runuser -u "$U" -- "$PY" -m app --print-sudoers --config "$CONFIG"` is non-empty into `tmp=$(mktemp -p /root broke-ai-coder.sudoers.XXXXXX)` (cleaned up by an EXIT trap), and contains `"($OC_USER)"`.
- If `$OC_USER` exists: its uid is not 0 and not `$U`'s uid, and it is in none of the groups `root sudo admin wheel adm docker lxd disk shadow`. For a privileged group, print the `gpasswd -d` instruction; the script does not remove groups itself.
- If `$H/.config/broke-ai-coder/secrets.env` exists, it is a regular non-symlink file owned by `$U`.
- If `WORKING_DIRECTORY` exists, it is a directory and not a symlink.

**Phase B: actions.** All are idempotent; each checks state, then prints OK or runs.
1. Create group `brokeai` if it is missing.
2. Add `$U` to `brokeai` if needed. Remember to tell the user to log in again.
3. `useradd --create-home --home-dir /home/$OC_USER --shell /bin/bash --user-group $OC_USER` if the user is missing. Add `$OC_USER` to `brokeai`. `chmod 700` its home.
4. `chmod 700 "$H"`, printing the old mode.
5. `runuser -u "$U" -- mkdir -p "$H/.config/broke-ai-coder"`, `chmod 700` on that directory. If the secrets file is missing: `runuser -u "$U" -- touch` it, then `chmod 600`. If it exists: `chmod 600`. Content is never read.
6. WORKING_DIRECTORY:
   - if missing: `install -d -m 2770 -o "$U" -g brokeai`;
   - else `chown "$U:brokeai"` and `chmod 2770` on the directory itself only;
   - then `setfacl -m g:brokeai:rwX` and `setfacl -d -m g:brokeai:rwX` on the directory itself.
   - Non-recursive. If it is non-empty, print a note pointing to docs C5 for existing content.
7. Parent of SERVER_DIRECTORY: `install -d -m 0755 -o root -g root` if missing. SERVER_DIRECTORY itself: `install -d -m 2770 -o "$OC_USER" -g brokeai`.
8. `/etc/wsl.conf`:
   - `merged=$(runuser -u "$U" -- "$PY" -m app.ops.wslconf merge /etc/wsl.conf)`, and get the change list via `check`.
   - If there are no changes: OK.
   - Otherwise:
     - copy the existing file to `/etc/wsl.conf.bak-broke-ai-coder-$(date +%Y%m%d%H%M%S)`;
     - write `$merged` to a `mktemp -p /root` file;
     - `install -m 0644 -o root -g root` it to `/etc/wsl.conf`;
     - set `NEED_SHUTDOWN=1`.
   - Also set `NEED_SHUTDOWN=1` if `/proc/sys/fs/binfmt_misc/WSLInterop*` reads `enabled`.
9. Sudoers:
   - `visudo -cf "$tmp"`;
   - if `/etc/sudoers.d/broke-ai-coder` exists and `cmp -s`: OK;
   - else: back up the existing file to `/root/broke-ai-coder.sudoers.bak-<ts>`, then `install -m 0440 -o root -g root "$tmp" /etc/sudoers.d/broke-ai-coder`, then `visudo -c`;
   - if the final `visudo -c` fails: restore the backup (or remove the new file) and exit 1.
10. OpenCode user's git identity (docs C6 step 5):
    - only if `/home/$OC_USER/.gitconfig` does NOT exist. Run each command as `setsid -w runuser -u "$OC_USER" -- env HOME="/home/$OC_USER" git config --global ... </dev/null 2>&1 | cat`:
      - `user.name "broke-ai-coder agent"`;
      - `user.email agent@example.invalid`;
      - `--add safe.directory '*'`.
    - If the file exists: print `exists; not touched`.

**Phase C: summary.** Print:
- the changed items;
- `NEXT:` steps:
  - in PowerShell `wsl --shutdown`, then reopen Ubuntu (if `NEED_SHUTDOWN` is set or the group changed);
  - fill in the secrets file (`nano`; names in `secrets.env.example`);
  - `scripts/live-checks.sh`;
  - `scripts/start-daemon.sh`.
- The git version advice: if `git --version` is below 2.45.1 (compare with `sort -V`), print `sudo add-apt-repository ppa:git-core/ppa && sudo apt update && sudo apt install -y git` as an instruction only.

### 4.6 `scripts/live-checks.sh` (read-only; run as the daemon user, not root)

Refuse with exit 2 if run as root. If any of `DISCORD_BOT_TOKEN`, `OPENROUTER_API_KEY`, `GEMINI_API_KEY` or `GROQ_API_KEY` is set, warn, print the names only, and exit 2: "run it from a shell without secrets".

Helpers:
- `as_oc() { setsid -w sudo -n -u "$OC_USER" -- "$@" </dev/null; }`. Never let it see a tty.
- `pass`/`fail`/`skip`/`manual`/`info` print `[PASS] L1 ...`.
- The values come from `setup_values`, run as the current user.

The script must not contain any of these (enforced by test): `sudo -S`, `read -rs`, `rm -`, `chmod`, `chown`, `setfacl -m`, `useradd`, `usermod`, `install -`, `tee `, `mkfifo`, `> /etc`.

Checks, in order:
- **L0**:
  - WSL;
  - `$PY` present;
  - `OPENCODE_BIN` root-owned and not group/other-writable;
  - git >= 2.45.1 (FAIL plus the PPA instruction as text);
  - `python -m app.ops.wslconf check /etc/wsl.conf` → PASS/FAIL.
- **L1**:
  - `sudo -n true` must FAIL (→ PASS);
  - `as_oc "$OPENCODE_BIN" --version` must succeed;
  - **positive control** `as_oc /usr/bin/test -d "/home/$OC_USER"` must return 0. If it does not, mark every remaining brokeoc check SKIP (`sudo rule not working`), because sudo's own failure is also rc 1.
  - MANUAL: `sudo visudo -c`.
- **L2**: `OPENCODE_SERVER_USERNAME=x FOO=y as_oc /usr/bin/head -c 4096 /proc/self/environ | tr '\0' '\n' | cut -d= -f1` contains `OPENCODE_SERVER_USERNAME` and not `FOO` (print names only).
- **L11**: the same output has no `XDG_DATA_HOME`.
- **L3/N1**: daemon PID from the PID file.
  - If the daemon is running:
    - for each `pgrep -u "$OC_USER" -f 'opencode serve'` pid, `ps -o tty= -p` must be `?`;
    - `as_oc /usr/bin/test -p /proc/<pid>/fd/1` must return 0 (pipe).
  - If not running: `pgrep -u "$OC_USER"` must be empty.
- **L4**:
  - with `rc=$(as_oc /usr/bin/test -r X)`, rc 1 is PASS;
  - for `/usr/bin/head -c 1 X`, a non-zero rc is PASS.
  - Targets: the secrets file, `$HOME` (also `-x`), `DATABASE_PATH`, `/mnt/c`, and `/proc/<daemon pid>/environ` when the daemon is running.
  - MANUAL: the `.exe`/`sh -c` checks; see TASKS T082 L4.
- **L5**: `stat -c '%G %a' "$WORKING_DIRECTORY"` = `brokeai 2770`, and `getfacl -p` shows `default:group:brokeai:rwx`. MANUAL: the agent-commit part.
- **L6** (read-only part): `as_oc /usr/bin/test -e /home/$OC_USER/.local/share/opencode/auth.json` returns 1 → PASS. MANUAL: the planted-file test.
- **L7**:
  - `/proc/sys/fs/binfmt_misc/status` exists, and every `WSLInterop*` entry's first line is `disabled`;
  - for each `/run/WSL/*_interop` socket, `as_oc /usr/bin/test -w SOCKET` must return 1;
  - `info` with `cat /proc/sys/dev/tty/legacy_tiocsti`;
  - MANUAL: `/init` and `.exe` execution.
- **L8**: MANUAL.
- **L9**: `getent passwd "$OC_USER" | cut -d: -f7` = `/bin/bash`. MANUAL: `.cache`.
- **L10**:
  - if the daemon is running, `as_oc /usr/bin/test /proc/<serve pid>/cwd -ef "$SERVER_DIRECTORY"` returns 0;
  - MANUAL: the run cwd.
- **check-os-user**: `"$PY" -m app --check-os-user --config "$CONFIG"` → PASS/FAIL.
- **L12**: MANUAL. Refer to the TASKS L12 prompts.
- Final line: the counts. Exit 1 if any FAIL.

---

## 5. T102: `scripts/systemd/broke-ai-coder.service` (example, not installed)

```
[Unit]
Description=broke-ai-coder agent controller daemon
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=@USER@
WorkingDirectory=@REPO@
ExecStart=@REPO@/scripts/start-daemon.sh
KillMode=mixed
KillSignal=SIGTERM
TimeoutStopSec=60
Restart=on-failure
RestartSec=10
# Exit 2 (bad config) and 3 (startup/isolation check failed) need a human: no restart loop.
RestartPreventExitStatus=2 3
UMask=0077
# Do NOT add NoNewPrivileges=: the daemon needs setuid sudo for the OpenCode user.
# Secrets are loaded by start-daemon.sh only: do NOT add EnvironmentFile=.

[Install]
WantedBy=multi-user.target
```

Document the installation in the docs (Part F):
1. `sed "s|@USER@|$USER|; s|@REPO@|$HOME/broke-ai-coder|" scripts/systemd/broke-ai-coder.service | sudo tee /etc/systemd/system/broke-ai-coder.service`
2. `sudo systemctl daemon-reload`
3. `sudo systemctl enable --now broke-ai-coder`
4. `journalctl -u broke-ai-coder -f`

Unverified assumption, added to T082 as L13: the distro boots only when something starts WSL, and whether it stays up with only systemd services running is unverified. Starting WSL at Windows logon (Task Scheduler) is documented as a live-check item. T102 stays PARTIAL.

---

## 6. Docs

**README.md**
- Add `## Quick start (WSL, production)` at the top, as a numbered end-to-end list. Each step links to the setup guide part:
  1. Discord app (A);
  2. provider keys (B);
  3. packages (C2);
  4. OpenCode (D1);
  5. clone, venv and `config.yaml` (E1);
  6. `sudo scripts/wsl-setup.sh --yes`, then `wsl --shutdown`;
  7. fill in the secrets from `secrets.env.example`;
  8. `scripts/live-checks.sh` and `scripts/health-check.sh`;
  9. `scripts/start-daemon.sh` (tmux) or the systemd unit;
  10. the first `/code` (D3);
  11. `/status`, `/usage`, `/models`, `/health`;
  12. a security-notes pointer (SPEC §11, deny list D2c).
- Keep the Windows dev section as `## Development (Windows/tests)`.
- Remove the stale note "Discord and OpenCode are not implemented yet".

**docs/SETUP-discord-opencode.md**
- C4: say the names match `secrets.env.example`. The start command becomes `scripts/start-daemon.sh`.
- C5/C6: add a "Fast path" note: after D1 and E1, `sudo scripts/wsl-setup.sh --yes` performs C5, C6 steps 1-5 and E2. The manual steps stay as reference.
- New **D2c, OpenCode permissions**:
  - the default deny categories;
  - `opencode.permission.extra_bash_deny`;
  - the defaults cannot be removed;
  - deny is stricter than approval; Discord approval buttons (T061) are future work;
  - a guardrail, not a boundary (bypasses; OS user is the boundary);
  - project files must not contain `permission`.
- D5: point to Part F.
- E2: add "or let wsl-setup.sh do it".
- E4: `scripts/start-daemon.sh`.
- New **Part F, Operate**: start, stop, health-check, live-checks, bench (`.venv/bin/python scripts/bench_idle.py --seconds 1800`), the systemd unit (section 5), and the git PPA advice.
- Troubleshooting rows:
  - `does not deny the required operations` → P3/L12;
  - `key 'permission' is not allowed in project config`;
  - start-daemon refusals (secrets mode or format, already running).

**SPEC.md**
- §5.2: add "Current implementation": deny by default, approval is T061 future, deny is stricter, the trusted-project exemption is not supported yet.
- §11: add **R5**, the deny list as a guardrail with its bypasses.
- §16: an example `opencode.permission.extra_bash_deny: []`.

**ARCHITECTURE.md**
- Line about `GET /config` (around line 235): add the permission check.
- §11.1: add the `/health` and bench note.

**config.example.yaml**: under `opencode:` add the following, plus extend the SAFETY comment with one line about permission:

```yaml
  permission:
    # Extra OpenCode bash patterns to DENY on top of the built-in list (git push, recursive rm,
    # deploy, global installs, credential changes). The built-in list cannot be removed.
    extra_bash_deny: []
```

---

## 7. TASKS.md sync (verify each module exists before marking)

Use these formats: `**Status:** DONE`, `**Status:** DONE (offline; live check in T082)`, `**Status:** PARTIAL: <note>`. Do NOT add a second line containing `OC() {`; `tests/test_opencode_sweep_proc.py:138` requires exactly one.

**DONE:**
- T001, T001A, T011, T012, T013, T020, T021, T021A, T022, T023, T030, T031, T034, T035, T040, T041, T042, T043, T050, T051, T051A, T052, T053, T054, T055, T063, T070, T071, T073, T080, T081, T090, T091, T092;
- T094 and T100 (`secrets.env.example`);
- T101: update the text to say no `start-opencode` script is needed (the daemon manages serve); the scripts are start-daemon, stop-daemon and health-check;
- T103.
- Fix T071's duplicate Status line.

**DONE (offline; live check in T082):** T003, T032. (T010 is PARTIAL: `/approve` and `/deny` are missing — list it under PARTIAL only.)

**PARTIAL:**
- T010: `/approve` and `/deny` are not implemented (T061).
- T024: metrics, `/health` and the bench script exist; the 30-minute idle run and the representative workload measurement are pending live.
- T044: refresh runs lazily before tasks, rate-limited by `provider_refresh_interval_s`; there is no background job.
- T060: sensitive commands are DENIED by OpenCode permission rules with a fail-closed preflight; approval requests are T061.
- T084: offline parts are tested; the ledger check after the E2E run is pending.
- T093: controller-only `/health`; there are no live provider probes.
- T102: system unit example; WSL boot persistence is unverified (L13).

**TODO:** T002 (the user's WSL lacks opencode), T061, T082, T083.

**BACKLOG:** T110-T114 (already).

T082: append L12 (P1-P6), as checks:
- serve starts;
- the preflight passes;
- a `/code` asking to run `git push` gets a denied tool result, and the task finishes without hanging;
- `cd . && git push` is denied;
- reading `/etc/hostname` is denied (external_directory) while edits inside the project work;
- websearch is absent or denied;
- note the `question` tool's behaviour.

Also append L13 (T102 boot persistence) and add `scripts/live-checks.sh` as the automated subset of L0-L11.

---

## 8. Tests (offline; `.venv/Scripts/python -m pytest`, ruff check + format clean)

- **`tests/test_opencode_permissions.py`**:
  - block shape: `"*"` first and in the dumped JSON;
  - every default is present with `deny`;
  - extras are appended and de-duplicated;
  - validator rejections: empty, `*`, braces, control characters, more than 200 characters;
  - unknown key `bash_deny` rejected;
  - `build_opencode_config` contains the block;
  - `permission_problem` returns its key path for every rule in 1.1 (cases 1-6 and the agent cases), and an agent `{"edit": "deny"}` is OK;
  - `check_server_config` gives UNSAFE with "permission" in the detail and no pattern text in the detail;
  - the superset/subset cases in 1.5;
  - `OpenCodeExecutor.check_server_config` with a config that has extras against a MockTransport serving the defaults-only body → UNSAFE.
  - Copy the patterns from `tests/test_opencode_config_check.py` (`probe`, `mutated`).
- **`tests/test_project_config_check.py`** (append): a project `permission` key → UNSAFE; agent permission bash `allow` → UNSAFE; agent permission `{"edit": "deny"}` → OK.
- **`tests/test_health.py`**:
  - `read_rss_bytes` on a fake status file, and on a missing path → None;
  - `read_peak_rss_bytes` returns None when `sys.platform` is monkeypatched to `win32`;
  - `ProcessSampler` with injected clock, cpu and rss: percentages, window None below 1 s;
  - `format_health`: the label `excludes OpenCode`, `unavailable on this platform`, the FATAL line truncated, and a secret placed in `serve_fatal_error`/`os_user` is redacted via `SecretRedactor`;
  - `run_command("health")` authorized vs `Not authorized.` (extend `FakeBackend` in `tests/test_bot_commands.py` with `health()`);
  - `ControllerBackend.health` with fake probes;
  - `DirectLauncher`/`SudoLauncher.status_summary` with a fake probe (copy the probe from `tests/test_opencode_os_user.py`).
- **`tests/test_bench.py`**:
  - `main(["--seconds","0.3","--interval","0.1","--warmup","0","--no-discord-import","--config",<example>])` returns 0 or 1, and the output contains `excludes OpenCode`;
  - a subprocess runs `[sys.executable, "scripts/bench_idle.py", "--seconds","0.5","--interval","0.1","--warmup","0"]` with cwd=ROOT and env containing `DISCORD_BOT_TOKEN=<sentinel>`. Expect rc in (0, 1), the sentinel absent from the output, and no new `*.db` in ROOT/data from the run (check that the bench DB path is in a temp dir).
- **`tests/test_ops.py`**:
  - every `merge_wsl_conf` case listed in 4.4, plus idempotence and CRLF;
  - `check` CLI exit codes (0/1/2), using a tmp file;
  - `setup_values` for `config.example.yaml`: the keys and their order;
  - an unsafe value (`working_directory: "/work space"`) exits 2;
  - a ConfigError exits 2.
- **`tests/test_scripts.py`**:
  - the files exist: the 5 `.sh` files, `bench_idle.py` and the unit;
  - each `.sh` has the shebang, `set -eu` in the first 15 lines, and no `\r\n`;
  - the git index mode is `100755` for `*.sh` (skip if git is unavailable);
  - `bash -n` with a POSIX bash found as follows, and skip if none: `shutil.which("bash")`, rejecting paths containing `system32` or `windowsapps` (the WSL launcher), or else `C:\Program Files\Git\bin\bash.exe`;
  - `shellcheck -S warning` if it is on PATH, else skip;
  - with that bash: `wsl-setup.sh` with no args → rc 2 and output mentions `--yes`; each script `--help` → rc 0;
  - the forbidden tokens in `live-checks.sh` (4.6);
  - `wsl-setup.sh` contains no `source`, no `. "$` and no `cat` of `secrets.env`;
  - `start-daemon.sh` contains `exec "$PY" -m app`;
  - the unit has `User=@USER@`, `RestartPreventExitStatus=2 3`, and no `NoNewPrivileges=yes` or `EnvironmentFile`;
  - `secrets.env.example`: every non-comment line matches `^export [A-Z_][A-Z0-9_]*=$`, and the name set equals the config names (section 3).
- Existing suites must stay green, in particular `tests/test_opencode_server.py` (`"sk-"`/`API_KEY` absent), `tests/test_discord_frontend.py` (the command set) and `tests/test_bot_format.py`.

## Out of scope

- Approval buttons and an approval DB (T061).
- Live provider probes in `/health`.
- psutil or any new dependency.
- Running any script against the real WSL.
- Checks of `.opencode/agent/*.md` or plugins (documented as R5).
- Recursive ACL fixes.
