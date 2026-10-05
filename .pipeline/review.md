# Review (round 3): S4 fix (pkill -v), N-doc1, N-test1, sid validation (feat/opencode-user, uncommitted on a601282)

VERDICT: SHIP

S4 now works. `sweep_stray` lists the OpenCode user's processes from /proc through the HostProbe. It takes every session id except the serve's and runs `pkill -KILL -U <name> -s sid1,sid2,...` as the OpenCode user. The argv is valid for procps-ng, and the tests pin the real argv plus a flag allowlist. N-doc1 and N-test1 are done. The sid <= 0 bug the tester found is fixed in two places. Nothing from the previous round has regressed. R1 stays NARROWED until the T082 live checks pass.

## Gate, re-run by the reviewer (`.venv/Scripts/python`)
- `timeout 450 python -m pytest -q`: **1418 passed, 1 xfailed** (103.3 s), exit 0. This matches both of the tester runs.
- No `fake_opencode`/`fake_serve` process was left after the run (Win32_Process command-line scan).
- `ruff check .`: clean. `ruff format --check .`: 138 files already formatted.
- `python -m app --check --config config.example.yaml`: OK.
- No `pkill ... -v` and no `--inverse` remain anywhere in code or docs. The only `-v` hit is the allowlist comment.

## Live argv check (WSL Ubuntu-24.04, procps-ng 4.0.4, signal 0 only, no sudo)
| Command | rc | Meaning |
|---|---|---|
| `pkill -0 -U nobody -s 999998,999999` | 1 | valid syntax, no match |
| `pkill -0 -U $me -s 999998,$$` | 0 | the comma list works and matches the listed session |
| `pkill -0 -U root -s $$` | 1 | `-U` and `-s` are ANDed, so the user filter really narrows |
| `pkill -0 -v -s 1` | 2 | `-v` is rejected (confirms the old bug) |
| `pkill -0 -s 1,abc` | 2 | a non-int sid is rejected |
| `pkill -0 -U $me -s 0` | 0 | `-s 0` means the session of pkill itself |

The last row shows the sid > 0 filter is necessary and correct: without it a `0` would target the sudo/pkill session.

## Verification of each item
- **S4: DONE.**
  - `parse_stat_session` (`app/opencode/os_user.py`) splits after the LAST `)` and reads field 6 (index 3 after state). It returns None for anything malformed, non-int or <= 0.
  - `SystemHostProbe.user_processes` filters by real uid (`Uid:` field 1), which is what `pkill -U` matches. Entries that vanish or cannot be read or parsed are skipped, and a missing /proc returns [].
  - `sweep_stray` is a no-op in each of these cases: no serve is registered, the user is unknown, the listing raises (logged by exception type only), or no other session remains. It filters `sid > 0` again and excludes the serve session.
  - It runs as the OpenCode user, so the kernel limits it to processes of that user in any case.
  - The executor call (`finally`, `shield`, `suppress`) is unchanged.
- **S4 tests: meaningful.**
  - The real argv is checked with a comm that contains `) S 1 2 3 4 (`, and with duplicate sessions de-duplicated and sorted.
  - There are no-op tests for: only the serve session left, a /proc failure, an unregistered serve and an unknown user.
  - The allowlist test covers every pkill path: sweep_stray, sweep, both kill_tree paths and probe reap. A self-test proves the checker rejects `-v`.
  - The sid-injection test covers `-v`, `7;rm`, `0`, `-5` and `6000,1`.
  - A fake-/proc test checks real vs effective uid and the bad-entry cases.
  - The tester mutations (a) to (f) were all caught. Removing only one of the two redundant sid guards is not caught; that is acceptable as defense in depth.
- **sid fix: DONE.** It is applied in both the parser and `sweep_stray`, and the signal-0 check above confirms it is needed.
- **N-doc1: DONE.**
  - TASKS L4 `OC` is on one line with `printf '%s\n' "$PW"`.
  - `test_tasks_oc_helper_is_one_valid_line` guards it.
  - The L3 S4 bullet now describes the /proc plus `-U -s` mechanism and the residuals.
- **N-test1: DONE.**
  - `FAKE_OC_MAX_LIFETIME_S` (default 180 s) caps three things: fake `serve` (via an `os._exit` timer thread), the `spawn_child` heartbeat loop, and the `spawn_child` parent sleep.
  - Each cap has its own test.
  - The `started_manager` teardown stops every manager and re-raises the first error afterwards. It has no direct test, which is acceptable for fixture code.
- **N-race1: open, documented as a follow-up** (low, self-healing).

## Non-blocking notes (new)
- **N-perf1 (low).** `sweep_stray` calls the synchronous `user_processes` on the event loop. That means two small file reads per /proc entry, once per run. This is negligible on a normal host. Use `asyncio.to_thread` if it ever shows up in profiles.
- **N-live1 (add to T082 L3).** `sweep_stray` kills every brokeoc session other than the serve session. If `opencode serve` itself starts helpers in their own session (for example LSP or MCP servers started detached/`setsid`), they would be killed after every run. That is fail-safe, but it degrades function. During L3, run `ps -o pid,sid,cmd -u brokeoc` while serve has LSP/MCP helpers running, and confirm they share the serve sid.

## Follow-ups (carried forward, current status)
1. **R1: NARROWED, not yet closed.**
   - The code is correct: no daemon home, secrets, DB or environ access. Interop fails closed, including when binfmt_misc is unmounted.
   - Round-1 Required 1-4 are DONE in code and docs.
   - R1 becomes RESOLVED only once T082 L1-L11 pass live, including:
     - L3: pty/pipe check, run sweep (S4, now the /proc mechanism; add N-live1), FIFO probe reap;
     - L4: non-tty denial checks;
     - L7: /run/WSL sockets, `/init` exe, binfmt mounted and disabled, systemd-binfmt, legacy_tiocsti.
   - Per R1d, any interop success keeps R1 OPEN on WSL.
2. **T082 live checks (open, deferred):**
   - L1 sudoers/no-root;
   - L2 env_keep;
   - L3 sids, pty/pipe, run sweep (S4 plus N-live1), probe-timeout reap (S3), N1 after stop;
   - L4 non-tty denial as brokeoc;
   - L5 ACLs plus the agent commit, without running git in /workspace;
   - L6 planted auth.json;
   - L7 interop, binfmt, sockets, `/init`, tiocsti;
   - L8 sudo journal;
   - L9 SHELL and .cache;
   - L10 cwd;
   - L11 as listed in TASKS.
3. **Round-1 Required 1 (use_pty): DONE.**
4. **Round-1 Required 2 (git workflow docs): DONE.**
5. **Round-1 Required 3 (guide order, set -e): DONE.**
6. **Round-1 Required 4 (interop live checks, unmounted binfmt): DONE** in code and docs; the live part is in T082 L7.
7. **S1: DONE.**
8. **S2: DONE.**
9. **S3: DONE.**
10. **S4: DONE** in code and tests (the round-2 Required fix). The live part is in T082 L3.
11. **Sid <= 0 validation (tester finding): DONE.**
12. **Round-2 nits:**
    - N-doc1: DONE.
    - N-test1: DONE.
    - N-race1 (sweep vs. serve relaunch): open, low.
    - N-perf1: open, low.
    - N-live1: open, in T082 L3.
13. **N1: RESOLVED** for serve restart and stop. Runs are covered by S4, pending the L3 live check.
14. **N4:** now R1b (documented).
15. **N9 and N10: RESOLVED.**
16. **N2:** moot in sudo mode; open, low, for mode none.
17. **N3, N5, N6, N7 (open, low):** unchanged.
18. **F1:** fixes 1 and 3 SUPERSEDED, fix 2 DONE. Fix 4: command/project provider DONE, alias/baseURL SUPERSEDED.
19. **Resolved earlier:** F2, F3, F4, F7, F10, F17, F18, F19, N8.
20. **Superseded earlier:** F11, F20-F22.
21. **F5, F6, F8, F9, F12-F16 (open):** unchanged.
22. **F23 (open, in T082):** check whether GET /config echoes the gateway apiKey and can serve as a serve identity check. Confirm the merge order of enabled_providers.
23. **F24:** keys SUPERSEDED. Plugin code is contained to brokeoc. The docs forbid running opencode as brokeoc from a terminal, and E3 `auth list` uses the non-tty form.
24. **Known gap (open):** a session cancelled before its first event is not aborted server-side.
25. **Deferred (open):**
    - approvals T060/T061;
    - start scripts T101/T102;
    - live E2E T082;
    - B4;
    - T044;
    - concurrent tasks (the S4 sweep assumes one run at a time per OpenCode user).
26. **Slice-2 follow-ups and the strict xfail (open):** unchanged.
27. **Residuals, documentation only (open):**
    - the WSLg X11 socket (/tmp/.X11-unix);
    - Windows-host and localhost services (mirrored networking, a Docker Desktop TCP API) are outside the OS-user boundary;
    - a process the agent starts through serve (in the serve session) survives until the next serve reap or daemon stop (documented in L3).
