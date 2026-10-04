# Test results: T072 OpenCode as a separate Linux user (sudo) + N1, N9, N10

Result: PASS. No application code was changed by the tester.

- `timeout 400 .venv/Scripts/python -m pytest -q`: 1363 passed, 1 xfailed (122 s). Baseline before audit: 1320 + 1 xfailed.
- `ruff check .`: clean. `ruff format --check .`: 137 files already formatted.

## Added: `tests/test_opencode_os_user_audit.py` (43 tests; coder's 94 tests in `test_opencode_os_user.py` were audited and kept)

Gaps found in the coder's tests and filled:
- Real `OpenCodeExecutor` + `SudoLauncher` with a captured `create_subprocess_exec`: argv is `sudo -n -u brokeoc -- /abs/opencode ...`, with no `-E`, shell or root target. Prompts `-x --help`, `--dangerously-skip`, `; rm -rf ~ $(id)`, and `-u root -- /bin/sh` stay one trailing element after `--`. Spawn kwargs: `stdin=DEVNULL`, `close_fds` default, session or process-group flags, cwd is the project dir.
- Env through sudo with a hostile daemon environ (provider keys, bot token, AWS, HOME, XDG_*, daemon PATH): exactly {PATH=/usr/bin:/bin, LANG, three OPENCODE_*}, no secret values, FOO dropped. Same check for the managed serve `_spawn` (argv, env, cwd = server_directory).
- N9 where the reason flips from None to set between two attempts on one executor: the second attempt returns `EXECUTOR_UNAVAILABLE` and does not spawn. A relative command under sudo also does not spawn.
- Each of the 9 privileged groups individually fails closed. Daemon home with a trailing slash. `/home/justin2` is not "under" `/home/justin`. The first failing access probe stops the sequence. Error messages carry no env secrets.
- `SystemHostProbe.interop_enabled` against a fake binfmt dir, which the coder never exercised: WSLInterop and WSLInterop-late enabled, mixed, both disabled, unrelated entries, empty or garbled first line (fails closed), unreadable entry (fails closed), empty dir. Also `killpg` name mapping, pgid<=0 guard and never-raise, and exact `run` spawn kwargs (stdin DEVNULL, PIPEs, env, `start_new_session`, no shell).
- Every pkill goes through sudo as the OpenCode user and selects only `-s <launch pid>` or `-U brokeoc`; it never selects the daemon user, and no killpg pgid <= 0.
- Startup via the real CLI in sudo mode (platform faked as non-Linux): exit 3, `ISOLATION_FAILED` message, no secrets and no traceback in stderr, no probe run, no sweep. `--check-os-user` with the real launcher: exit 3, no sweep. `--once` builds the launcher but registers no services. The default mode is sudo.

## Mutation checks (each backed up byte-exact, mutated, tests run, restored, restored file confirmed identical to the backup)

All 42 below were KILLED (at least one test failed), and the restored file was byte-identical afterwards:
- `SudoLauncher.command`: drop `-n`; insert `sh -c`; insert `-E`; drop `--`; target `root`.
- `SudoLauncher.environment`: pass all extras; add HOME; honour `passthrough`; use the daemon PATH.
- `self_check`: skip the interop check; interop check ignores WSLInterop-late; skip the `/proc/<pid>/environ` probe; skip the protected-paths probes (`/mnt/c`, secrets); skip the DB probe; skip the daemon-home `-x` probe; treat rc None or other as pass; skip the privileged-group check; allow oc uid == daemon uid; allow uid 0; skip the server_dir `-w` check; skip the passthrough check; sweep inside the self-check.
- `start()` without sweep; `stop()` without sweep.
- Auth store: head failure returns None; exception returns None.
- Sudoers: runas `ALL`; drop `env_reset`; skip name validation; skip path validation.
- Kill paths: sweep targets the wrong user; `kill_tree` without the `-KILL` pkill; `kill_tree` without the `-TERM` pkill.
- `build_launcher` always Direct; `DirectLauncher.start` without the warning.
- Executor: N9 guard removed; N9 moved before the invalid-request check; the timeout kill bypasses the launcher; the argv bypasses `launcher.command`.
- Server: `_reap` sweep removed; N10 text with the restart instruction moved after the fatal text (killed by `-k unavailable_reason`).

Notes:
- A first mutation attempt had arguments in the wrong order and left `app/opencode/os_user.py` corrupted. I restored it from the byte-exact backup and confirmed with `cmp` that all three mutated files match their backups.
- With the N10 mutation, running the full server and e2e files together hung until the 120-250 s timeout (the failure is not cleanly reported there). The targeted N10 unit test fails correctly. The same was seen once with the reap mutation under a 300 s cap, but it was KILLED on rerun (55 s).
- Timed-out runs left 4 orphan `fake_opencode.py serve` python processes. I killed exactly those (command line matched `fake_opencode.py serve`), and 0 remain.

## Not verifiable offline (unchanged)

The T082 L1-L11 live checks (real sudoers/visudo, sid == sudo pid, env_keep, WSL binfmt names) remain live-only. `interop_enabled` is verified against a fake binfmt directory, not the real WSL one.
