# Changes (review fix round: S4, N-doc1, N-test1)

## S4 - `pkill -v` removed (procps-ng pkill has no -v)
- `app/opencode/os_user.py`: new `parse_stat_session()` (session id = field 6 of /proc/<pid>/stat, parsed after the LAST `)` so comm with spaces/parens is safe); new `HostProbe.user_processes(uid) -> [(pid, sid)]`, implemented in `SystemHostProbe` from /proc (`Uid:` real uid; vanished/unreadable/unparseable entries skipped).
- `SudoLauncher.sweep_stray`: lists brokeoc's sessions, drops the registered serve's session, and if any remain runs `pkill -KILL -U brokeoc -s sid1,sid2,...` via sudo as brokeoc. No-op with no registered serve, no other sessions, unknown user, or a listing failure (logged, never raises).
- Tests (`tests/test_opencode_os_user.py`): FakeHostProbe gets `oc_stats` (raw stat lines) and `proc_error`; real-argv test with comm containing spaces and `)`; only-serve-session no-op; proc-failure no-op; `parse_stat_session` unit test; allowlist test over every pkill argv the launcher can produce (sweep_stray, sweep, kill_tree both paths, probe reap) plus a self-test that the checker rejects `-v`.
- `TASKS.md` L3 (S4 bullet) now describes the /proc-listing mechanism.

## N-doc1
- `TASKS.md` L4: `OC` helper is now one line, `printf '%s\n' "$PW"` (was a literal line break). Rest of the helper checked; no other breaks.

## N-test1
- `tests/fakes/fake_opencode.py`: `FAKE_OC_MAX_LIFETIME_S` (default 180) caps `serve` (timer thread `os._exit(0)`) and the `spawn_child` heartbeat loop.
- `tests/test_opencode_server.py`: `started_manager` teardown stops every manager, re-raising the first error afterwards; new test that a fake serve exits by itself with cap 1 s.

## N-race1
- No code change; remains a documented follow-up.

## Verification
- `pytest -q` twice: 1401 expected (1400 passed + 1 xfailed before the extra lifetime test); ruff check and format --check clean. No fake_opencode/fake_serve processes left after either run.
- Mutations (each caught): re-add `-v` -> test_sweep_stray_spares_the_serve_session fails; drop serve-session exclusion -> same test fails; parse stat by first `)` -> same test fails; remove lifetime cap -> new max_lifetime test fails (it was NOT caught by the old suite, hence the new test). Files restored afterwards.

## Tester focus
- Linux-only live check (TASKS L3 S4) still needed; /proc scan is untested on a real host.
