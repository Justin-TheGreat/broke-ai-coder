# Review: go-live readiness round (feat/go-live, uncommitted on 331ae69)

VERDICT: NEEDS WORK

Most of the round is correct and well tested:
- T060 permission rules and the fail-closed preflight;
- `/health` and self-metrics;
- the bench;
- the secrets template;
- start, stop and health-check scripts;
- `live-checks.sh`;
- the systemd example;
- the docs and the TASKS sync.

With the default config (`/workspace`, `/var/lib/broke-ai-coder/opencode-server`), `wsl-setup.sh` is safe on the user's real WSL. It cannot lock out sudo: the file is checked with `visudo -cf`, then `visudo -c` runs and a failure rolls back. It keeps `[boot]` and makes backups, does not run repo Python as root, and leaves secret content untouched.

It is not safe for a mistyped or unusual config. The script runs as root and chowns, chmods and ACLs two directory paths taken from `config.yaml` with no normalization and no system-path guard. A single config value can therefore give the agent user (brokeoc) root, or break R1. That has to be fixed before the user runs the script for real.

## Gate, re-run by the reviewer
- `timeout 500 .venv/Scripts/python -m pytest -q`: **1587 passed, 5 skipped (shellcheck absent), 1 xfailed** (119 s), exit 0. No leftover fake_opencode/fake_serve processes.
- `ruff check .`: clean. `ruff format --check app tests scripts`: 137 files already formatted.
- `bash -n` (Git Bash) is OK on all 5 scripts. They have no CR bytes. The index mode is 100755 for `scripts/*.sh` and `bench_idle.py`; the unit is 100644 (intent-to-add, which is correct).
- `wslconf.merge_wsl_conf` was exercised by hand on the user's actual `/etc/wsl.conf` (`[boot]\nsystemd=true`) and on CRLF, mixed-case, quoted, duplicate and inline-comment inputs. `[boot]` is preserved, and the second merge is always a no-op.
- The user's WSL was inspected read-only (no state changed). User `jichen`, uid 1000, home `0750`. git **2.43.0**. sudo 1.9.15p5. **`setfacl`/`getfacl` are not installed.** No brokeoc, no `/workspace`, and no clone yet. `ps -o user=` prints full names (procps 4.0.4), so the stop-daemon owner check is fine.

## Required fixes

### R-1 (BLOCKING for a real run). Normalize and guard WORKING_DIRECTORY and SERVER_DIRECTORY before root touches them
Where: `scripts/wsl-setup.sh` Phase A, lines 202-214 and 242-246. The values are used as root at lines 327-339 (`chown`, `chmod 2770`, `setfacl -m`/`-d -m`) and 341-354 (`install -d -m 2770 -o "$OC_USER" -g brokeai`). Python side: `app/ops/setup_values.py:18` and `app/opencode/os_user.py:393-395` (`_under`).

The defect has four parts:
- `setup_values` allows `.` and `/`, so `..` passes.
- The "not under `$H`" check (line 211) is a plain string prefix.
- There is no system-directory guard.
- SERVER_DIRECTORY has no symlink check, and neither parent is checked.

`chown`, `chmod`, `setfacl` and `install -d` all dereference symlinks.

These cases can be shown from config alone. All of them pass Phase A today:
- `working_directory: /tmp/../home/jichen`.
  1. Step 4 sets `$H` to 700.
  2. Step 6 then runs `chown jichen:brokeai`, `chmod 2770` and `setfacl g:brokeai:rwX` on `/home/jichen`.
  3. brokeoc can now write your home, so it can replace `~/broke-ai-coder/.venv` or the scripts. That is code execution as the daemon user, and R1 is broken.
- `server_directory: /var/lib/../../etc`. This runs `install -d -o brokeoc -g brokeai -m 2770 /etc`. brokeoc then owns `/etc`, which gives the agent root.
- `working_directory: /usr/local` (or `/`, `/opt`, `/var/lib`, ...). The `brokeai` group (the agent) gets write access on a directory that is in root's `secure_path`, which leads to root.
- `server_directory` inside `working_directory` (for example `/workspace/.serve`). The agent can plant a symlink there. The next "safe to re-run" invocation then chowns the target of that symlink to brokeoc.

Fix. Add the following to Phase A, applied to both paths. Each failure goes through `problem`, so nothing is changed.
1. `canon=$(realpath -m -- "$path")` must equal `$path`. This rejects `..`, `.`, `//`, a trailing `/` and symlinked components.
2. Refuse `/`, and refuse a path equal to or under any of: `/bin /boot /dev /etc /lib /lib32 /lib64 /libx32 /proc /root /run /sbin /snap /sys /usr /tmp /var/tmp /mnt /home`. Also refuse a path exactly equal to `/var`, `/var/lib`, `/opt`, `/srv` or `/media`.
3. Reject SERVER_DIRECTORY if it is a symlink, or if it exists and is not a directory. Mirror lines 242-246.
4. For each path, the nearest existing ancestor must be owned by root and must not be group- or other-writable (`stat -c '%U %a'`). The agent then cannot plant a symlink before or during the run.
5. Neither path may equal or contain the other.
6. Neither path may equal or contain `$REPO_DIR` or `dirname "$DATABASE_PATH"`.

Also do the following:
- Make `_under` in `os_user.py` compare `posixpath.normpath` values, so the daemon's own self-check is not fooled by `..` either.
- Optionally, reject non-normalized absolute paths in `setup_values` (exit 2).
- Add tests:
  - Python: `setup_values` and `_under` with `..`, `//` and trailing `/`.
  - Shell: either a stubbed-PATH run of Phase A (fake `id` that returns 0, `getent`, `runuser`, `stat`, `realpath`), or at minimum static assertions that the guards exist and come before `ERRORS`. Cover these cases: `/tmp/../home/u`, `/`, `/usr/local`, `/etc`, server dir inside working dir, and a symlinked server dir.

### R-2. Docs: the quick start is not orderable on the user's machine
`docs/SETUP-discord-opencode.md:105`. The C2 `apt install` list has no `acl`. The user's WSL has no `setfacl`, so README step 6 (`sudo scripts/wsl-setup.sh --yes`) always refuses ("command setfacl is missing"). The refusal is safe but avoidable. Add `acl` to the C2 package line.

## Recommended (not blocking)
- **wsl-setup.sh:121.** Validate `$H`: absolute, not `/`, a directory, not a symlink, owned by `$U`. Step 4 runs `chmod 700 "$H"`, and an account whose home is `/` or a system directory would be bricked.
- **wsl-setup.sh:296-308, 409.** For an existing OC user, check that its passwd home is `/home/$OC_USER`. Steps 3 and 10 assume that path; otherwise `chmod 700` aborts halfway through Phase B.
- **wsl-setup.sh:410-421.**
  - `| cat` hides git's exit status, so line 420 reports "set the git identity" even when it failed.
  - git runs with cwd `$REPO_DIR` inside your 700 home.
  - Fix: run it as `(cd / && ...)` and afterwards require that `/home/$OC_USER/.gitconfig` exists.
- **wsl-setup.sh:395.** `install` writes straight into `/etc/sudoers.d`. That is safe in practice: the file was already checked with `-cf`, there is a rollback, and WSL can be recovered with `wsl -u root`. It would be atomic if you installed to `/etc/sudoers.d/.broke-ai-coder.new` (sudo ignores names containing a dot) and then did `mv -f`.
- **wsl-setup.sh:387.** The "up to date" path does not check the file's owner and mode. Also require `stat -c '%U %a'` = `root 440`.
- **wsl-setup.sh:365-371.** Refuse to install an empty `$merged`.
- **wsl-setup.sh:2.** Add an ERR trap that prints the failing line and says a re-run is safe. Today a mid-Phase-B failure exits 1 with no summary.
- **live-checks.sh:205.** Use `sudo -n -k true`. Cached credentials (for example right after `sudo wsl-setup.sh` without `wsl --shutdown`) otherwise give a false FAIL, "passwordless sudo for everything". E2 already runs `sudo -k` first.
- **README / docs quick start.**
  - The user's git is 2.43.0, so L0 in `live-checks.sh` will FAIL. Put the git-core PPA step into quick-start step 3 (C2), not only into Part F and the wsl-setup summary.
  - README anchors for Parts A and B are wrong: GitHub strips the `≈`, so the anchors are `#part-a--discord-15-min-from-a-pc-browser` and `#part-b--provider-api-keys-15-min`.
- **setup_values.py:52.** `_SAFE_RE.match` with `$` accepts a trailing newline. Use `fullmatch`.
- **start-daemon.sh:65/104.** A relative `--config` is checked before `cd "$REPO_DIR"` and used after it. Make it absolute, as `wsl-setup.sh` does.
- **TASKS.md T072 (line 693).** Change "DONE" to "DONE (offline; live check in T082)", consistent with R1 NARROWED.
- **systemd unit.** `Wants=network-online.target` can stall WSL boot on `systemd-networkd-wait-online` (a known WSL issue). Note this under L13.

## Verified OK (no action)
**T060**
- `"*": "allow"` comes first, followed by denies only. Last-match-wins is therefore safe, and the preflight enforces "only index 0 may be `*`", so `ask` or a late `allow` gives UNSAFE.
- `required_permission=None` still requires the defaults. The executor passes the configured extras.
- Detail strings carry key paths only.
- Project `permission` and weakening `agent`/`mode` entries are UNSAFE.
- The validator rejects empty entries, `*`, braces, control characters and entries over 200 characters.
- An agent-writable global config that adds keys moves `"*"` out of first place, so the result fails closed rather than allowing more.
- R5 is documented honestly in SPEC §11, D2c and the module docstring (guardrail, not boundary; the OS user is the boundary). P1-P6 are in T082 L12.

**/health**
- It goes through `authorize` and `safe_text`, with no URL, path or env value.
- It degrades on Windows ("unavailable on this platform").
- `status_summary` never raises.

**start-daemon.sh**
- It refuses root, a symlinked or foreign-owned secrets file, a mode other than 600/400, malformed lines (printing line numbers only) and a second daemon.
- Secrets are sourced only right before `exec`.
- The PID survives the exec.

**stop-daemon.sh** checks the cmdline `-m app` and the owner before signalling.

**systemd unit**
- It has `User=`, `RestartPreventExitStatus=2 3` (matching cli exit 2 for config and 3 for StartupError, and start-daemon refusals), `KillMode=mixed`, and no `NoNewPrivileges` or `EnvironmentFile`.
- `sed` without `g` is fine, since there is one placeholder per line.

**live-checks.sh**
- It is read-only. `--check-os-user` runs `self_check` only, which never sweeps.
- It refuses root and set secret variables (names only).
- The positive control gates every brokeoc denial check, and the sudoers rule allows `/usr/bin/test` with any arguments, so the control is representative. Denial results become SKIP, never PASS, when sudo is broken.

**wsl-setup.sh**
- `--yes` gates everything, and Phase A collects every problem and exits 2 before any change. The `mktemp` in `/root` is removed by the EXIT trap.
- `SUDO_USER` must be set, not root, and must exist.
- `wsl.conf`: backup, then `install` 0644.
- Sudoers: `visudo -cf`, backup, `install` 0440, `visudo -c`, rollback.
- Every `"$PY"` call goes through `runuser -u "$U"` (D5). Secrets are only created or chmodded, never read.
- Steps are idempotent, and nothing is recursive.
- With the default config, no agent-writable directory is an ancestor of a path that root touches. `/` and `/var/lib/broke-ai-coder` are root-owned and 0755.

## Residual risks (documented; not blocking)
- **R5.** The deny list can be bypassed by `bash x.sh`, interpreters, `curl | sh`, plugins, `.opencode/agent/*.md` and `git -C dir push`. The boundary is brokeoc.
- **Scripts not executed.** The shell scripts are covered only by static tests and refusal runs; Phase B, start, stop and live-checks have never been executed (a stub-sandbox harness is recommended, see R-1). The first real run is the user's.
- **Phase A to Phase B window.** A TOCTOU on WORKING_DIRECTORY exists in principle, but it cannot be exploited with root-owned parents. R-1 item 4 makes that a precondition.
- **`[interop] enabled=false`.** This removes `code .`, `explorer.exe` and similar from WSL shells. C6 step 4 documents the cost.

## Follow-ups (carried forward, current status)
1. **R1: NARROWED, not closed.**
   - Code and docs are as in round 3. `wsl-setup.sh` now automates C5, C6 1-5 and E2. `live-checks.sh` automates the read-only subset of L0-L11.
   - R1 stays NARROWED until the T082 live checks pass (L1-L11, including L3 pty/pipe and run sweep with N-live1, L4 non-tty denials, and L7 interop/binfmt/sockets/`/init`/tiocsti) and R-1 above is fixed.
   - Per R1d, any interop success keeps R1 OPEN.
2. **T082 live checks: open.**
   - L1-L11 as before.
   - New: **L12** (T060 P1-P6: serve accepts `permission`; preflight passes; `git push` and `cd . && git push` denied without a hang; `external_directory` denied; websearch absent or denied; `question` behaviour).
   - New: **L13** (T102 boot persistence, Task Scheduler fallback).
   - Run `scripts/live-checks.sh` after `wsl --shutdown`.
3. **Round-1 Required 1-4, S1-S4, sid validation: DONE.** The live parts are in T082 L3 and L7.
4. **Round-2/3 nits.**
   - N-race1: open, low.
   - N-perf1: open, low.
   - N-live1: open, in T082 L3.
   - N-doc1 and N-test1: DONE.
5. **N1:** RESOLVED for serve and stop; the run part is pending L3.
6. **N9 and N10:** RESOLVED.
7. **N4:** documented as R1b.
8. **N2:** open, low (mode none).
9. **N3, N5, N6, N7:** open, low.
10. **F1:** fixes 1/3 SUPERSEDED, fix 2 DONE, fix 4 partly DONE and partly SUPERSEDED.
11. **Resolved earlier:** F2, F3, F4, F7, F10, F17, F18, F19, N8.
12. **Superseded earlier:** F11, F20-F22.
13. **Open, unchanged:** F5, F6, F8, F9, F12-F16.
14. **F23:** open, in T082 (whether GET /config echoes the apiKey; enabled_providers merge order). L12 (P3) also covers `permission` key order in GET /config.
15. **F24:** keys SUPERSEDED; contained to brokeoc.
16. **Known gap (open):** a session cancelled before its first event is not aborted server-side.
17. **Deferred work.**
    - T060: now PARTIAL (deny rules plus preflight).
    - T061 approvals: open.
    - T101: DONE.
    - T102: PARTIAL (L13).
    - T024: PARTIAL (30-min idle run pending live).
    - T093: PARTIAL (no live provider probes).
    - T044: PARTIAL (lazy refresh).
    - T082, T083: open. B4: open.
    - Concurrent tasks: open (the S4 sweep assumes one run per OpenCode user).
18. **Slice-2 follow-ups and the strict xfail:** open, unchanged.
19. **Residuals, documentation only (open):** the WSLg X11 socket; Windows-host and localhost services; agent processes in the serve session live until the next reap.
20. **New this round:** R-1 and R-2 (required), plus the recommended list above.
