# Test results: go-live readiness round (feat/go-live)

Status: ALL PASS. No application code or script was changed; only tests were added.

## Full runs
- `timeout 500 .venv/Scripts/python -m pytest -q`: 1587 passed, 5 skipped (shellcheck not installed), 1 xfailed (pre-existing), 156 s.
- `ruff check .`: clean. `ruff format --check app tests scripts`: clean (137 files).
- Git index modes of `scripts/*.sh` and `bench_idle.py` are still 100755 (nothing staged or committed by this run).

## Tests added
- `tests/test_opencode_permissions.py`: exact default list head/tail/samples, no duplicates, tuples; config cannot remove or override defaults (extras equal to a default stay one deny, unknown keys rejected); no `sk-`/`API_KEY`/`TOKEN`/`{env:` in the block; an end-to-end preflight table (missing permission, `"*"` last, later allow, appended allow, missing pattern, each denied tool as allow or ask, agent bash allow, mode websearch allow), each asserting UNSAFE, the key path in the detail, and no pattern text; non-required appended rules must be deny (ask, allow, None, True, 0).
- `tests/test_project_config_check.py`: any `permission` value in a project file is UNSAFE; agent and mode weakening variants name the key path only.
- `tests/test_health.py`: `/health` through `run_command` with hostile probes (bot token, provider key, gateway token, server password, endpoint, path) is redacted and has no database path; real backend output has no URL, path, "token" or "password"; Windows-like platform degrades to "unavailable on this platform"; unauthorized users never reach the backend.
- `tests/test_scripts.py`: a structural read-only scanner for `live-checks.sh` (strips comments, heredocs and quoted text, then flags mutating commands, `sed -i`, `sudo` without `-n`, `git` subcommands, and redirections to anything but `/dev/null` or `&N`), with its own self-test showing it catches `useradd`, `chmod`, `tee`, `> /etc/...`, `>> /usr/...`, `cp`, `install`, `sudo useradd`, `setfacl`, `rm`, `git push`. `as_oc` may only run the OpenCode binary, `/usr/bin/test` or `/usr/bin/head`. The positive control comes before every denial check, sets `SUDO_OK=0` only in its else branch, and every later `as_oc`/`denied_read` call has a `need_sudo` guard (skip, never pass). Root and secret-variable refusal prints names only. `wsl-setup.sh`: the `--yes` gate precedes every other action; Phase A has all required preconditions, no mutating commands, and exits 2 before Phase B; idempotence guards, backup-before-install for wsl.conf and sudoers, rollback, no recursive chmod/chown/setfacl; no key names, no reader command on the secrets path; every `"$PY"` use goes through `runuser -u "$U"`. A real run of `wsl-setup.sh --yes` under Git Bash with `SUDO_USER` empty or `root` refuses with exit 2, "nothing was changed", and no `[setup] RUN:` line. `start-daemon.sh`: `set -eu`, no `export`/`eval`, secrets sourced between `set -a` and `set +a` after all refusals and right before the final `exec`, only line numbers printed for malformed lines. `secrets.env.example` has names only. TASKS.md statuses match the spec lists, exactly one `OC() {` line and one Status line per task; spot checks of the code behind the claims (no `/approve` in `COMMAND_NAMES`, lazy refresh in the orchestrator, new modules exist).

## Mutation checks (byte-exact backup, mutate, run, restore, `cmp` identical every time)
All were killed (at least one test failed) and every file was restored byte-identical.
- permissions.py: drop `index == 0` (killed); `value != DENY` to `value == ALLOW` (initially SURVIVED, killed after adding `test_non_required_appended_rule_must_also_be_deny`); remove required-pattern loop; remove agent/mode loop; `"*"` last in the block; drop `doom_loop`.
- config_check.py: remove the `permission_problem` call; make `required` ignore `required_permission`; remove the project `permission` branch.
- models.py validator: allow `*`; allow braces.
- live-checks.sh: delete `SUDO_OK=0`; make `need_sudo` always succeed (initially SURVIVED, killed after adding `test_need_sudo_is_driven_by_the_positive_control_flag`); add `chmod`; add `echo x >/etc/foo`; add `useradd`.
- wsl-setup.sh: no-`--yes` exits 0; a Python call without `runuser`; allow `SUDO_USER=root`; Phase A errors exit 0; add `cat "$SECRETS"`.
- start-daemon.sh: add `export` after `set +a`.
- format.py: drop `safe_text` from `format_health`; drop the `[:200]` truncation.

## Findings for the Reviewer (no failures)
- The pre-existing forbidden-token test for `live-checks.sh` is a plain substring scan. It misses `>` redirects to paths, `cp`, `mv`, `dd`, `sed -i`, `/usr/bin/tee` variants and the like. The new structural scanner covers those.
- Behavioral execution of `start-daemon.sh`, `stop-daemon.sh`, `live-checks.sh` and the later phases of `wsl-setup.sh` was not done (no stubbed-command sandbox exists in the repo tests, and running them for real was forbidden). They are covered by static and syntax checks plus the refusal runs above.
- `wsl-setup.sh` creates its sudoers render with `mktemp -p /root` during Phase A (before the error exit); it is removed by the EXIT trap. This is a temp file, not a system change.
- No tests could be run for the 5 shellcheck cases (not installed).
