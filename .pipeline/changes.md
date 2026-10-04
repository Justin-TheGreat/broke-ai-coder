# Changes: T072 OpenCode as a separate Linux user (sudo) + N1, N9, N10

Verified on Windows: full suite `1320 passed, 1 xfailed`; `ruff check .` and `ruff format --check .` clean;
`-m app --check --config config.example.yaml` OK; `-m app --print-sudoers` (with os.name/user lookup faked as
posix/justin) prints text identical to the spec and to `EXPECTED_SUDOERS` in the tests; on Windows it exits 2
with "run --print-sudoers inside WSL as the daemon user".

## Files changed

New
- `app/opencode/os_user.py`: constants, `HostProbe` protocol, `SystemHostProbe` (lazy pwd/grp, signals by name,
  `run` = DEVNULL stdin, `env={"PATH": "/usr/bin:/bin"}`, `start_new_session=True`, bounded timeout),
  `OpenCodeLauncher` protocol, `DirectLauncher` (mode none = today's behaviour + one warning), `SudoLauncher`
  (command/environment/kill_tree/sweep/auth_store_problem/self_check/start/stop), `render_sudoers`,
  `build_launcher`. Assumptions S1-S5, P1, P2, W1 are in the module docstring.
- `tests/test_opencode_os_user.py`: 94 tests with `FakeHostProbe`/`FakeProc` (no real sudo).

Modified
- `app/config/models.py`: `OsUserConfig` (mode default `sudo`, name regex and not `root`, `protected_paths`
  validation); `OpenCodeConfig.os_user`.
- `app/opencode/auth_store.py`: split into `credential_providers_from_text` and `credential_problem`; behaviour unchanged.
- `app/opencode/config_check.py`: `check_server_config(..., auth_problem=)` async callable overrides the local read.
- `app/opencode/process.py`: `_wait` renamed to public `wait_quiet`.
- `app/opencode/server.py`: takes a launcher; serve argv/env/executable/auth check/kill go through it; `_reap`
  always sweeps afterwards (suppressed errors); isolated-only env_keep hint on the "does not enforce the password"
  message; N10 fatal text ("... will not restart by itself; fix the cause, then restart the daemon: <fatal>", capped
  at 300) and new fatal log line; docstring R1 paragraph replaced.
- `app/opencode/executor.py`: takes a launcher; `executable`, `command`, `environment`, `kill_tree` (both kill
  sites, shielded one still shielded), `auth_problem` passed to the config check; N9: after the invalid-request and
  project-dir checks and before the spawn, a non-None `unavailable_reason()` returns `EXECUTOR_UNAVAILABLE`.
- `app/runtime/wiring.py`: one launcher shared by manager and executor; `Runtime.launcher` (last field); services
  `[gateway, launcher, manager]` (`[]` for `--once`, launcher built but never started).
- `app/cli.py`: `--print-sudoers` and `--check-os-user` (check never sweeps; mode none exits 3).
- `config.example.yaml`: absolute `command`, `server_directory`, `os_user` block, `env_passthrough` comment.
- Docs: `docs/SETUP-discord-opencode.md` (tree, C4, C5 group workspace, new C6, D1, D2, D4, checklist,
  troubleshooting), `SPEC.md` (section 9 paragraph, R1 resolved + R1a-R1f), `ARCHITECTURE.md` (boundary diagram, R1,
  failure modes), `TASKS.md` (T072 DONE; T082 L1-L11), `README.md` notes.
- Tests: `conftest.py` (`os_user.mode: none` in base config), `test_wiring.py` (service lists),
  `test_cli.py` (mode none in the startup-error config; new CLI tests), `test_config.py`, `test_opencode_server.py`
  (RecordingLauncher, reap order, hint, N10), `test_opencode_executor.py` (N9, launcher argv, kill_tree),
  `test_opencode_config_check.py`, `test_managed_crash_window_e2e.py` (N9 fallback test; fatal test asserts
  "restart the daemon"), `test_bot_format.py`.

## Tester focus

1. sudo argv never allows a shell or arbitrary command: `SudoLauncher.command` is exactly
   `[/usr/bin/sudo, -n, -u, <name>, --, *argv]`; the prompt stays one element after `--`; sudoers allows only
   opencode path + pkill + test + head as `<oc>` (never root/ALL; names/path validated by regex).
2. Env passed to sudo is exactly `{PATH=/usr/bin:/bin, LANG?, OPENCODE_CONFIG_CONTENT, OPENCODE_SERVER_PASSWORD,
   OPENCODE_SERVER_USERNAME}`; extras outside `KEEP_ENV` dropped; `passthrough` ignored (self-check rejects non-empty).
3. Self-check fails closed on every listed condition (checks 1-13, in order, exact messages); rc None on an
   access probe fails; check 14 only warns; `self_check` never calls pkill; `start()` sweeps once only after success.
4. Kill paths: `kill_tree` order (killpg TERM, pkill -TERM -s, wait, pkill -KILL -s, killpg KILL, proc.kill) and the
   exited-proc case; `_reap` = kill_tree then sweep; none of these raise.
5. N9 (no spawn when `unavailable_reason()` is set; invalid prompt still INVALID first), N10 (message starts with
   "managed OpenCode server is down", contains "restart the daemon", <= 300), N1 (sweep on every reap/stop).
6. auth.json read as brokeoc: bounded `head -c 1048577`, anomalies = unreadable (fail closed), values never leak.
7. Reminder: mode `sudo` is the default; the config models do no OS-dependent validation.

## Suggested mutation checks (each should make a test fail)

- Insert `"-c"`/`"sh"` or drop `"--"` in `SudoLauncher.command`; add `ALL`/`root` runas to `render_sudoers`.
- Add `HOME` or a non-`KEEP_ENV` extra to `environment`; stop dropping extras; honour `passthrough`.
- Make `self_check` treat rc None as pass, skip the interop check, skip the `/proc/<pid>/environ` or `/mnt/c`
  probe, or call `sweep()` inside it; swap the order of checks.
- Remove the final `sweep()` from `_reap`; remove `KILL` step or `killpg` from `kill_tree`; make a probe exception propagate.
- Remove the N9 block in `executor.run`, or move it before the invalid-request checks.
- Put the fatal text before the restart instruction in `unavailable_reason()` (cap would cut it).
- Make `auth_store_problem` return None on `head` failure, oversized output, or decode error.
- Make `DirectLauncher` the default in `build_launcher` for mode `sudo`, or change the model default to `none`.

## Live-only checks (cannot be verified offline): T082 L1-L11

L1 visudo/no-password/`sudo -n true` fails. L2 env_keep exactness and serve 401/GET /config. L3 sid == sudo pid
(S1/S2), no leftovers after /cancel, timeout, daemon stop. L4 brokeoc is denied secrets, daemon home, daemon
/proc environ, /mnt/c, any .exe. L5 /workspace group+ACL sharing and agent commits. L6 planted auth.json blocks
startup and tasks. L7 no enabled WSLInterop* after wsl.conf change (W1). L8 sudo journal contains prompts. L9
shell tool (SHELL from passwd) and provider package download into /home/brokeoc/.cache. L10 serve/run cwd (S4/B8).
L11 auth.json default XDG path under env_reset (S5).

## Notes

- Python edit helpers were used in the scratchpad; some existing files were rewritten with LF endings while the
  working tree is CRLF (autocrlf=true); git normalizes this, no content change beyond the above.
- `.pipeline/spec.md` was already modified before this task and was not touched.
