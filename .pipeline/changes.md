# Changes: go-live readiness round (branch feat/go-live, on 331ae69)

Nothing committed or pushed. The only git operation was `git add` plus `git update-index --add --chmod=+x` on `scripts/*.sh` and `scripts/bench_idle.py` (staged, mode 100755), which `tests/test_scripts.py` requires. Everything else is unstaged.

Planner decisions D1-D5 were implemented as written. I found none wrong. Notes: D2 sets `doom_loop` to deny exactly as specified; D5 holds (every `"$PY" -m` line in `wsl-setup.sh` goes through `runuser -u`, enforced by a test).

## Files changed

### T060 permissions
- `app/opencode/permissions.py` (new): `DEFAULT_BASH_DENY` (spec order), `DENIED_TOOLS`, `bash_deny_patterns`, `build_permission_block` (`"*": "allow"` first), `default_permission_block`, `permission_problem`, `agent_permission_problem`. Docstring lists P1-P6 and the guardrail-not-boundary caveat.
- `app/config/models.py`: `OpenCodePermissionConfig.extra_bash_deny` with validator (strip; reject empty, over 200 chars, control chars, braces, bare `*`); `OpenCodeConfig.permission`.
- `app/opencode/server.py`: `build_opencode_config` adds `permission` after `small_model`; `_spawn` logs the one-line INFO count (no pattern text); docstring updated.
- `app/opencode/config_check.py`: `check_server_config(..., required_permission=None)` checks permission after the gateway shape and returns UNSAFE `server config key '<path>' does not deny the required operations`. `check_project_config` rejects a top-level `permission` and weakening `agent`/`mode` permission. Docstring B14.
- `app/opencode/executor.py`: passes `required_permission=build_permission_block(self._config.permission)`.
- `app/bot/format.py`: OPENCODE_UNSAFE_CONFIG text gets the "permission rules ... (setup guide D2c)" sentence.

### /health, self-metrics, bench (T093/T094/T024)
- `app/runtime/resources.py` (new): `read_rss_bytes`, `read_peak_rss_bytes`, `cpu_seconds`, `ResourceSample`, `ProcessSampler` (no reader raises).
- `app/runtime/diagnostics.py` (new): `RuntimeProbes`.
- `app/gateway/server.py`: `is_running()`.
- `app/opencode/os_user.py`: `status_summary()` on the Protocol, `DirectLauncher`, `SudoLauncher` (`_checked_at` set in `start()` after `self_check()`; live interop probe wrapped in try/except).
- `app/runtime/wiring.py`: builds `RuntimeProbes`, new last `Runtime.probes` field, passes it to `create_frontend`.
- `app/bot/discord_frontend.py`: `probes` param, `ControllerBackend(..., probes=...)`, registers `/health`.
- `app/bot/commands.py`: `"health"` in `COMMAND_NAMES`, `HealthSnapshot`, `BotBackend.health`, dispatch.
- `app/bot/backend.py`: `ControllerBackend.health()`.
- `app/bot/format.py`: `format_health`.
- `app/runtime/bench.py` (new) and `scripts/bench_idle.py` (new thin wrapper): idle benchmark with a temp DB, `environ={}`, `once=True` runtime, and a report with PASS/FAIL/UNKNOWN. Exit 1 only on FAIL.

### Ops, scripts, unit, templates
- `app/ops/__init__.py`, `app/ops/setup_values.py`, `app/ops/wslconf.py` (new): the `KEY=value` printer (values restricted to `[A-Za-z0-9._/+-]`) and the line-based wsl.conf merge/check CLI.
- `scripts/start-daemon.sh`, `stop-daemon.sh`, `health-check.sh`, `wsl-setup.sh`, `live-checks.sh` (new, LF, executable in index). `scripts/systemd/broke-ai-coder.service` (new).
- `secrets.env.example`, `.gitattributes` (new).
- `config.example.yaml`: `opencode.permission.extra_bash_deny: []` plus a SAFETY comment line.

### Docs
- `README.md` (Quick start, Development section, stale note removed), `docs/SETUP-discord-opencode.md` (C4, C5/C6 fast path, D2c, D5, E2, E4, Part F, three troubleshooting rows), `SPEC.md` (5.2, R5, section 16), `ARCHITECTURE.md` (`GET /config` permission check, 11.1), `TASKS.md` (status sync, T060/T093/T100/T101 text, T082 L12/L13 plus the live-checks note, T071 duplicate Status line fixed; still exactly one `OC() {` line).

### Tests
- New: `tests/test_opencode_permissions.py`, `tests/test_health.py`, `tests/test_bench.py`, `tests/test_ops.py`, `tests/test_scripts.py`.
- Extended: `tests/test_project_config_check.py` (3 permission tests), `tests/test_bot_commands.py` (`FakeBackend.health` plus an authorized/unauthorized `/health` test).

## Deliberate deviations and judgment calls
- `live-checks.sh` must not contain `install -` (enforced by test), so the git PPA instruction is printed as words (add ppa:git-core/ppa, apt update, upgrade git, "docs Part F") instead of the full command. `wsl-setup.sh` and the docs print the full command.
- `wsl-setup.sh` comment wording avoids the word "source" because the test forbids it.
- `wsl-setup.sh` accepts `-h/--help` (exit 0) and, without `--yes`, prints usage to stderr and exits 2. The `--help` output goes to stdout.
- `OPENCODE_BIN` mode check uses `stat -L` (follows symlinks); the spec said `stat -c '%U %a'`.
- A relative `--config` in `wsl-setup.sh` is made absolute before it `cd`s to the repo.
- `test_setup_values_cli_output` monkeypatches `os` inside `app.ops.setup_values`, because on Windows `os.path.abspath` yields a drive letter that the safe-value regex rejects. The scripts run on Linux only.

## Tester focus
1. `permission_problem` / `agent_permission_problem` rules 1-6 and 1.5 edge cases (leading `"*"` position, `ask` anywhere, superset/subset). No pattern text in any detail string.
2. The preflight is fail-closed by default: `check_server_config` with `required_permission=None` still requires the defaults. Existing test helpers produce a good body via `build_opencode_config`, so no old test needed changes.
3. `/health` output never contains an endpoint, password, token, env value or path; the redactor is applied through `safe_text`.
4. `wsl-setup.sh`: Phase A checks everything before any change; no `--yes` means exit 2 with no change. Only static and syntax checks were run on it; never run it here.
5. `live-checks.sh`: the positive control gates every brokeoc check (SKIP, not PASS, when sudo itself fails). It refuses root or any secret variable set.
6. `wslconf.merge_wsl_conf`: every occurrence fixed, insertion point, CRLF in/LF out, idempotence.
7. TASKS.md: statuses match the spec lists; `OC() {` count is 1 (`tests/test_opencode_sweep_proc.py`).

## Suggested mutation checks (each should make a test fail)
- Permission rules: in `permission_problem`, drop the `index == 0` condition, so a non-first `"*": "allow"` passes. Change `value != DENY` to `value == "allow"`, so `ask` passes. Remove the `for p in req_bash` loop, so a missing extra passes. Remove the `agent`/`mode` loop. Make `build_permission_block` put `"*"` last. Drop `doom_loop` from `DENIED_TOOLS`. Expect failures in `tests/test_opencode_permissions.py`.
- Preflight: remove the `permission_problem` call in `check_server_config`, or make `required` ignore `required_permission`. Expect `test_executor_requires_configured_extras` and `test_check_server_config_*` to fail. Remove the `"permission" in data` branch in `check_project_config`, and expect `tests/test_project_config_check.py` to fail.
- Validator: allow `*` or braces in `extra_bash_deny`.
- live-checks positive-sudo gate: set `SUDO_OK=0` never (delete that line), or delete `need_sudo` guards. `test_live_checks_has_positive_sudo_control_and_no_tty` fails on the first. Add `chmod` or `rm -` to `live-checks.sh` and `test_live_checks_is_read_only` fails.
- wsl-setup preconditions and `--yes`: make the no-`--yes` branch exit 0 (`test_wsl_setup_without_yes_refuses` fails). Run a `"$PY" -m ...` line without `runuser -u` (D5 test fails). Add `source` or `cat ... secrets.env` (test fails).
- `/health` secret-free: remove `safe_text` from `format_health` (the redaction test in `tests/test_health.py` fails). Print `os_user` or fatal error unredacted. Remove the `[:200]` truncation (the fatal-line length assertion fails).
- wslconf: fix only the first occurrence of a wrong value (`test_wrong_value_fixed_in_every_occurrence...` fails), or skip the `umask` append.

## Commands (all run from the repo root; Windows, `.venv/Scripts/python`)
```
timeout 450 .venv/Scripts/python -m pytest -q
.venv/Scripts/ruff check .
.venv/Scripts/ruff format --check .
.venv/Scripts/python -m app --check --config config.example.yaml
bash -n scripts/start-daemon.sh   # likewise stop-daemon, health-check, wsl-setup, live-checks
.venv/Scripts/python scripts/bench_idle.py --seconds 2 --interval 0.5 --warmup 0
```
Use Git Bash for `bash -n`, not the System32 WSL launcher. Skipped here because they are not installed: the `shellcheck` tests (5 skips).

## Results at time of writing
- `pytest -q`: 1546 passed, 5 skipped (shellcheck not installed), 1 xfailed (pre-existing), about 150 s.
- `ruff check .`: clean.
- `ruff format --check app tests scripts`: clean. Note: plain `ruff format --check .` also reports `.pipeline/spec.md` (the planner's file; ruff reformats the python code blocks inside it). That is not a code change of this round and was left alone.
- `python -m app --check --config config.example.yaml`: `config OK; schema version 2`.
- `bash -n` (Git Bash) on all five new scripts: OK. `scripts/bench_idle.py --seconds 2` runs and prints the report (RSS UNKNOWN on Windows, as designed).
