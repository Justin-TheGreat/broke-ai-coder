# Test results (fix round S4 / N-doc1 / N-test1): PASS

## Previous pass (summary)
Added `tests/test_opencode_sweep_proc.py` (17 tests). It found one app bug: `sweep_stray` could pass
session ids `0` or negative to pkill (e.g. `-s -5,0,5000,8000`). Non-integer fields were already dropped, so
there was no string injection.

## Sid bug: FIXED and verified
`parse_stat_session` in `app/opencode/os_user.py` now returns None for `sid <= 0`. `sweep_stray` also filters
`sid > 0`. `test_sweep_stray_sids_are_clean_positive_ints_and_never_inject` now passes.

## Tests added by the previous pass (tests/test_opencode_sweep_proc.py)
- parse_stat_session: comm with newline and parentheses; malformed input returns None.
- SystemHostProbe.user_processes against a fake /proc: filters by REAL uid, skips vanished, unparseable and
  missing-status entries and non-numeric names; missing /proc returns [].
- sweep_stray argv targets only brokeoc; sid validation; TASKS.md `OC()` helper is one valid line.
- Fake `spawn_child` heartbeat child stops by itself under `FAKE_OC_MAX_LIFETIME_S`.

## Test-only change this pass
`tests/fakes/fake_opencode.py`: the `spawn_child` parent now sleeps `min(120.0, max_lifetime_s())` instead
of a fixed 120 s, so it respects the lifetime cap.

## Full runs (timeout 450, `.venv/Scripts/python -m pytest -q`)
- Run 1: 1418 passed, 1 xfailed (103 s).
- Run 2: 1418 passed, 1 xfailed (103 s). This run includes the fake `spawn_child` change.
- After each run, and after the mutation checks, no python process with fake_opencode/fake_serve in its
  command line remained. The only matches were my own query commands.
- `ruff check .`: all checks passed. `ruff format --check .`: 138 files already formatted.

## Mutation checks (each backed up, mutated, tested, restored, restoration confirmed with `cmp`)
| Mutation | Result | Failing test |
|---|---|---|
| (a) re-add `-v` in sweep_stray pkill argv | KILLED | test_sweep_proc::test_sweep_stray_sids_are_clean_positive_ints_and_never_inject |
| (b) drop the serve-session exclusion | KILLED | test_opencode_os_user::test_sweep_stray_spares_the_serve_session |
| (c) parse stat by first `)` (`find` instead of `rfind`) | KILLED | test_sweep_proc::test_parse_stat_comm_with_newline_and_parens |
| (d) remove fake lifetime cap, heartbeat loop | KILLED | test_sweep_proc::test_fake_spawn_child_heartbeat_loop_is_capped |
| (d) remove fake lifetime cap, serve `expire` sleep | KILLED | test_opencode_server::test_fake_serve_exits_by_itself_after_max_lifetime |
| (e) real uid to effective uid in the /proc Uid parse (`split()[1]` to `[2]`) | KILLED | test_sweep_proc::test_user_processes_filters_by_real_uid_and_skips_bad_entries |
| (f) remove both new `sid > 0` checks | KILLED | test_sweep_proc::test_sweep_stray_sids_are_clean_positive_ints_and_never_inject |

Mutation (f) removing only ONE of the two sid checks is NOT caught: 257 tests passed for each single removal.
The two guards are redundant (defense in depth), so each one masks the other and no test can tell them apart.
This is expected and not a defect. It does mean neither guard is individually pinned, only the pair together.

## Gaps and notes
- The `started_manager` fixture teardown ("stops all managers even if one raises") has no direct test.
- /proc scanning on a real Linux host remains untested here (live check TASKS L3 S4).
- No application code was changed by the tester. All mutations were restored byte-exact.
