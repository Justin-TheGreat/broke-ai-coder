# Review: go-live readiness, fix round (feat/go-live, uncommitted on 26eff61)

VERDICT: SHIP

R-1 and R-2 are fixed correctly and completely. All recommended items are applied. The tests execute the real guard code and are mutation-checked. Nothing regressed.

`wsl-setup.sh` is now safe for a mistyped or hostile `config.yaml`. Every reviewer counter-example is refused in Phase A, before any change. I confirmed this with the real `realpath`/`stat` on the user's WSL, read-only.

R1 stays NARROWED until the T082 live checks pass.

## Gate, re-run by the reviewer
- `timeout 500 .venv/Scripts/python -m pytest -q`: **1712 passed, 5 skipped (shellcheck absent), 1 xfailed** in 123 s, exit 0.
- `ruff check .` is clean. `ruff format --check app tests scripts` reports 138 files already formatted.
- `bash -n` passes on wsl-setup, live-checks, start-daemon and stop-daemon. No script contains a CR byte.
- The index mode of `scripts/*.sh` is still 100755.
- No fake_opencode/fake_serve processes were left behind.

## Integrity check after the tester's hand revert
The tester restored `scripts/wsl-setup.sh` and `scripts/live-checks.sh` by hand, so I checked both against `git diff` from 26eff61.
- **wsl-setup.sh:** I read all 615 lines in full and checked every diff hunk against changes.md. There is no residue and no lost line.
  - Phase A: realpath equality (l.167-171), the `/` refusal, the 22-entry denylist loop (l.176-181), the strict `/var` rule (l.182), symlink and non-directory refusal (l.186-197), and the every-ancestor root/no-g+w/no-o+w check with `%03d` padding (l.200-211) are all present.
  - The `valid_home` call clears USER_OK and raises a problem (l.265-268).
  - The OC home passwd check is at l.347-353.
  - There are 5 `check_apart` calls (l.356, 361, 362, 363 gated on USER_OK, 364).
  - `realpath` and `dirname` are in the required-commands list. The `ERRORS` gate is unchanged (l.405).
  - Phase B: the atomic `install_sudoers` (dotted name, `visudo -cf`, `mv -f`) is present, and "up to date" requires a non-symlink, `cmp` equality and `root 440`.
  - The empty `$merged` die comes before the backup and install.
  - `oc_git` runs from / in a subshell with `|| die`, followed by the `.gitconfig` existence check.
  - `$OC_HOME` is used everywhere. `STEP` is set before every step, and the ERR trap uses errtrace.
  - Unchanged regions (sudoers rollback paths, `cmp`, `chmod 600`, the non-recursive chown/chmod/setfacl) match 26eff61 exactly.
- **live-checks.sh:** the diff against 26eff61 is exactly 2 lines: `sudo -n -k true` and the L0 message pointing at C2. Nothing else differs.

## Verification of R-1 on the real WSL
The guard block (l.116-234) was piped to `wsl -d Ubuntu-24.04 -- bash` with a stub `problem`. Only `realpath`, `stat` and `dirname` ran, so nothing was changed.
- **Pass:** `/workspace`, `/var/lib/broke-ai-coder/opencode-server`, `/var/lib/broke-ai-coder/x/y`.
- **Refused:**
  - `/tmp/../home/jichen` (realpath gives `/home/jichen`) and `/var/lib/../../etc` (gives `/etc`).
  - `/usr/local`, `/`, `/etc`, `/var/lib`, `/var/lib/broke-ai-coder` itself.
  - `/workspace/` and `//workspace`.
  - `/home/jichen`, `/mnt/c/x`, `/proc/1`, `/tmp/x`, `/srv/x`, `/opt/a`.
  - `/bin` and `/lib/x`: these are usrmerge symlinks on 24.04, so they fail as non-normalized.
- **Ancestor check, with the denylist emptied to isolate it:** `/tmp/foo` is refused (`root 1777`) and `/home/jichen/x` is refused (`jichen 750`).
- **valid_home:** `jichen /home/jichen` passes. `/` and `/etc` are refused.
- **ERR trap:** the trap with errtrace does not fire in conditional contexts (an assignment from a command substitution followed by `||`, a subshell followed by `||`, or `if !`). There are no spurious FAIL lines.

## Fix-by-fix assessment
- **R-1 items 1-6: DONE.**
  - Normalization: realpath equality, plus `is_normalized_absolute` in setup_values, which also handles POSIX `//`.
  - Denylist: a superset of the one requested. `/opt` `/srv` `/media` are refused as trees, and `/var` is refused except strictly below `/var/lib/broke-ai-coder/`.
  - Symlink and non-directory refusal for both paths.
  - Every existing ancestor is checked, which is stronger than nearest-only.
  - Server vs working directory, repo, DB dir, daemon home and OpenCode home containment, in both directions.
  - `_under` now normalizes, `fullmatch` replaces `match`, and setup_values exits 2 on non-normalized directories.
- **R-2: DONE.** `acl` is in the C2 apt line, README step 3 mentions it, and the precondition message says `sudo apt install acl`.
- **Recommended items, all DONE:**
  - `valid_home` and the OC home passwd check.
  - git identity handling (no pipe to cat, cwd /, `.gitconfig` verified).
  - Atomic sudoers install, with "up to date" requiring `root 440`.
  - Empty wsl.conf refused.
  - ERR trap with step and "nothing changed" vs "safe to re-run".
  - `sudo -n -k true`.
  - git PPA in C2.
  - README anchors, checked by a test that computes GitHub anchors.
  - start-daemon makes a relative `--config` absolute.
  - T072 status set to "DONE (offline; live check in T082)".
  - L13 and Part F note on network-online/wait-online.
- **Tests are meaningful.**
  - `tests/test_wsl_setup_guards.py` cuts the real guard block and the Phase A containment block out of the script and runs them in bash with filesystem stubs. These are not just static needles.
  - It covers 38 rejected paths, near-miss accepts (`/srvx`, `/homeless`, `/usrlocal`, `/data/work`), symlinked components, 7 unsafe ancestor modes including 1777, and the every-ancestor check.
  - The ERR trap is executed for real.
  - Each guard was mutation-checked by both the coder and the tester, and every mutation was KILLED.
  - The DB-dir and home containment are executable tests.

## Nits (optional, not blocking)
- **N-sud1. wsl-setup.sh:563-570.** If the backup itself fails `visudo -cf` during rollback, `install_sudoers "$BACKUP" || true` leaves the new (valid, `-cf`-checked) file in place, but the message still says "rolled back". This can only happen if the original file was already invalid, in which case sudo was already broken. Fix: print the actual state on that branch.
- **N-sud2. wsl-setup.sh:555.** "Up to date" checks owner and mode, but not the group. Low.
- **N-db1.** A database placed in `/var/lib/broke-ai-coder` itself is refused, because its directory contains the serve directory. This is intended and listed in changes.md. Mention it in the E1 config comments if users are likely to move the DB.

## Residual risks (documented; not blocking)
- **R5.** The deny list can be bypassed by `bash x.sh`, interpreters, curl piped to sh, plugins, `.opencode/agent/*.md` and `git -C dir push`. The boundary is the brokeoc user.
- **Scripts not executed.** Phase B of wsl-setup (useradd, setfacl, sudoers install/rollback, git identity), start, stop and live-checks have never run end to end. The guards are now executed in tests and on real WSL (read-only), but the first real Phase B run is the user's.
- **Phase A to Phase B TOCTOU.** This is now closed in practice: root-owned, non-writable ancestors are a precondition and are verified.
- **`[interop] enabled=false`.** This removes `code .`, `explorer.exe` and similar from WSL shells. C6 step 4 documents the cost.

## Follow-ups (carried forward, current status)
1. **R1: NARROWED, not closed.** R-1 is now fixed.
   - R1 stays NARROWED until the T082 live checks pass: L1-L11, including L3 pty/pipe and run sweep with N-live1, L4 non-tty denials, and L7 interop/binfmt/sockets/`/init`/tiocsti.
   - Per R1d, any interop success keeps R1 OPEN.
2. **T082 live checks: open.**
   - L1-L11.
   - L12: T060 P1-P6.
   - L13: T102 boot persistence plus the network-online note.
   - Run `scripts/live-checks.sh` after `wsl --shutdown`.
   - The user's git is 2.43.0, so the C2 PPA step is needed before L0.
3. **R-1 and R-2 (this round's required fixes): DONE.** The recommended items from the previous review are DONE.
4. **New nits:** N-sud1 and N-sud2 open, low. N-db1 open, doc only.
5. **Round-1 Required 1-4, S1-S4, sid validation: DONE.** The live parts are in T082 L3 and L7.
6. **Round-2/3 nits.**
   - N-race1: open, low.
   - N-perf1: open, low.
   - N-live1: open, in T082 L3.
   - N-doc1 and N-test1: DONE.
7. **N1:** RESOLVED for serve and stop; the run part is pending L3.
8. **N9 and N10:** RESOLVED.
9. **N4:** documented as R1b.
10. **N2:** open, low (mode none).
11. **N3, N5, N6, N7:** open, low.
12. **F1:** fixes 1/3 SUPERSEDED, fix 2 DONE, fix 4 partly DONE and partly SUPERSEDED.
13. **Resolved earlier:** F2, F3, F4, F7, F10, F17, F18, F19, N8.
14. **Superseded earlier:** F11, F20-F22.
15. **Open, unchanged:** F5, F6, F8, F9, F12-F16.
16. **F23:** open, in T082 (whether GET /config echoes the apiKey; enabled_providers merge order). L12 (P3) also covers `permission` key order.
17. **F24:** keys SUPERSEDED; contained to brokeoc.
18. **Known gap (open):** a session cancelled before its first event is not aborted server-side.
19. **Deferred work.**
    - T060: PARTIAL (deny rules plus preflight).
    - T061 approvals: open.
    - T101: DONE.
    - T102: PARTIAL (L13).
    - T024: PARTIAL (30-min idle run pending live).
    - T093: PARTIAL.
    - T044: PARTIAL.
    - T072: DONE offline; live check in T082.
    - T082, T083: open. B4: open.
    - Concurrent tasks: open (the S4 sweep assumes one run per OpenCode user).
20. **Slice-2 follow-ups and the strict xfail:** open, unchanged.
21. **Residuals, documentation only (open):** the WSLg X11 socket; Windows-host and localhost services; agent processes in the serve session live until the next reap.
