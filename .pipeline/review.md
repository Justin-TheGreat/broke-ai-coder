# Review: T072 OpenCode as a separate Linux user (sudo) + N1, N9, N10 (feat/opencode-user, uncommitted on 420467c)

VERDICT: NEEDS WORK

The application code matches the spec and is careful:
- argv goes after `--` with no shell;
- the environment sent to sudo is an exact allowlist;
- every probe fails closed;
- `kill_tree`, `sweep` and the auth probe never raise;
- N9 and N10 are correct;
- `--check-os-user` never sweeps.

The sudoers rule cannot be turned into root or daemon-user execution (analysis below). Tests are meaningful: the tester’s 42 mutations were all killed, and the suite is green.

Two things stop R1 from being closed in practice on WSL, both in what the guide tells the user to do:
- the generated sudoers file turns off sudo’s pty isolation for every command run as brokeoc, including the user’s own interactive ones;
- the documented workflow ("push as yourself", "the daemon user’s `git status` works") runs agent-controlled git config and hooks as the daemon user.

Both are concrete paths from the agent (brokeoc) back to the daemon user (and from there to `secrets.env`, and to root through the user’s sudo password). On top of that, the setup guide cannot be followed in the order it is written.

All required fixes are small: one line of generated sudoers, its test, and docs.

## Gate, re-run by the reviewer (`.venv/Scripts/python`)
- `timeout 400 python -m pytest -q`: **1363 passed, 1 xfailed in 122.5 s, exit 0.**
- `ruff check .`: clean. `ruff format --check .`: 137 files already formatted.
- `python -m app --check --config config.example.yaml`: OK.
- No leftover `fake_opencode`/`fake_serve` processes after the run (0 found; nothing killed).

## Required fixes

### 1. `use_pty` is disabled, which hands brokeoc the user’s terminal (agent → daemon user / root)
`app/opencode/os_user.py:552` emits `Defaults>{oc_user} [not]use_pty` (the line that negates use_pty). A runas-scoped Default applies to EVERY `sudo -u brokeoc ...`, not just the daemon’s spawns.

**Why it matters.** Ubuntu’s global `Defaults use_pty` exists for exactly this case: a program run through sudo must not keep the invoking terminal. The sudoers man page gives the rationale: a background child can keep the tty after the command exits.

The docs tell the user to run agent-controlled programs as brokeoc from their own terminal:
- TASKS.md:774 L4 `sudo -u brokeoc bash`, which runs `/home/brokeoc/.bashrc`, and the agent can write that file;
- docs/SETUP-discord-opencode.md:255 and :258 `opencode auth list` / `auth logout`, which load `/home/brokeoc/.config/opencode` config and plugins (agent-writable);
- the checklist at :328.

**The chain.** With use_pty off, those processes share the user’s controlling tty and session. An agent-planted payload forks a brokeoc process that survives the command. That process:
- calls `tcsetpgrp` and reads the next keystrokes, for example the user’s sudo password (this needs no TIOCSTI);
- or uses TIOCSTI to inject commands into the user’s shell, if `dev.tty.legacy_tiocsti` is on; the Kconfig default is y.

The L6 scenario makes this worse. When the daemon refuses to start because of a planted auth.json, the troubleshooting path sends the user straight to `sudo -n -u brokeoc -- opencode auth logout`.

**Turning use_pty off buys the daemon nothing.** P2 (os_user.py:27-29) already guarantees that every daemon-side sudo has no controlling tty (`start_new_session=True`, stdin DEVNULL). sudo only allocates a pty when it runs in a terminal.

**Fix:**
- (a) Remove the use_pty-negating line from `render_sudoers` (os_user.py:552). Update `EXPECTED_SUDOERS` (tests/test_opencode_os_user.py:148), the spec text and the P2 docstring (os_user.py:27-29).
- (b) Add a live check to TASKS.md L3 (line 773): during a task, `ps -o pid,sid,tty,user,cmd -u brokeoc` shows tty `?` for `opencode run`/`serve`, and `ls -l /proc/<pid>/fd/1` is a pipe. That proves pipes and S2 are unchanged with Ubuntu’s default `use_pty`.
- If L3 ever shows a pty under the daemon, fall back instead to a command-scoped Default that negates use_pty only for the four command paths (`Defaults!<cmnd list>`), plus the doc rule in (c).
- (c) Docs, whichever option is chosen: never run an agent-controlled program as brokeoc from your own terminal. That covers bash, opencode, git, and anything that reads brokeoc’s dotfiles.
  - For D2 (lines 255, 258) and checklist :328, use `setsid -w sudo -n -u brokeoc -- /usr/local/bin/opencode auth list </dev/null 2>&1 | cat`.
  - To clear credentials, use `sudo -u brokeoc rm -f /home/brokeoc/.local/share/opencode/auth.json` (rm is not agent-controlled) instead of `opencode auth logout`.
  - Rewrite TASKS.md:774 L4 to use the same pattern instead of `sudo -u brokeoc bash`, for example `sudo -n -u brokeoc -- /usr/bin/head -c1 <path> </dev/null`, or run it before the agent has ever run.
  - Also fix the checklist text at :328: "(after `opencode auth logout`)" should name the sudo form.

### 2. The documented git workflow runs agent code as the daemon user
**The problem.** /workspace is group-writable by brokeoc (C5 ACLs), so the agent controls every repo’s `.git/config` and `.git/hooks`. That includes `core.fsmonitor`, `core.hooksPath`, `core.sshCommand`, `credential.helper`, filter, diff and textconv drivers, `include.path`, and the pre-commit and pre-push hooks.
- None of these appear in a worktree diff, so "review before running" (R1a) does not protect against them.
- `git status` alone runs `core.fsmonitor`. `git push` runs pre-push and `core.sshCommand`.

**Where the docs steer the user into it:**
- docs/SETUP-discord-opencode.md:201 "push as yourself";
- TASKS.md:775 L5 "The daemon user’s `git status` works".

Doing either after a task means arbitrary code as the daemon user, which reads `secrets.env` and undoes T072.

**Fix (docs only):**
- In C6 step 5 (line 201), and in R1a in SPEC.md:453 and ARCHITECTURE.md:728, state plainly:
  - never run git, tests, builds or an editor/IDE git integration (VS Code Remote-WSL included) as yourself inside /workspace after the agent has touched it;
  - `.git/config` and hooks are agent-controlled and invisible in diffs.
- Give the safe publish path: keep your own clone outside /workspace, `git fetch file:///workspace/<repo> <branch>` into it, review with `git diff`/`git log -p` there, and push from there. Note git >= 2.45.1 for the local-clone CVE fixes.
- Change TASKS.md:775 L5 to verify group/ACL sharing with `ls -l`/`getfacl` and the agent’s own commit, not a git command run as the daemon user.

### 3. The setup guide cannot be followed in order
The C6 steps run before the things they depend on exist:
- C6 step 6 (docs/SETUP-discord-opencode.md:207-217) runs `cd ~/broke-ai-coder` and `.venv/bin/python -m app --print-sudoers --config config.yaml`. The clone, the venv and `config.yaml` are only created in Part E (lines 295-318).
- C6 step 7 (:218-225) runs `/usr/local/bin/opencode --version`, but that copy is only made in D1 (line 242).
- D2 (:255) needs the sudoers rule from C6 step 6.

Following the guide top-down therefore fails at C6.6, then at C6.7 and D2. This is fail-closed, not a hole, but it is the one-time setup.

Fix: keep C6 steps 1-5 where they are. Move steps 6-7 (sudoers and verify) into Part E, after `config.yaml` is created and before the first start, with D2 after them. Alternatively, put an explicit "do D1 and E1-E2 first" gate at the top of C6.6.

While there:
- prefix step 6 with `set -e` (or abort unless `[ -s "$tmp" ]`). Without it, a failing `--print-sudoers` leaves an empty file, which `visudo -cf` accepts and `install` copies. That is harmless, but it confuses the step-7 result.

### 4. Live checks do not cover the interop escape the self-check cannot see
**The gap.** `interop_enabled()` (os_user.py:182-195) only reads the binfmt entries. The documented WSL bypass does not need binfmt: the per-session interop server sockets (`/run/WSL/*_interop`) and direct `/init <exe>` invocation. Whether those are refused when `[interop] enabled=false` is not verifiable offline.

The checks also depend on two unproven conditions:
- `/proc/sys/fs/binfmt_misc` must actually be mounted when it is read; an empty, unmounted directory reads as "disabled";
- under systemd, `systemd-binfmt` must not re-register anything.

**Fix (TASKS.md:774 L4 and :777 L7).** As brokeoc, using the non-tty pattern from fix 1:
- `ls -l /run/WSL/` shows no socket brokeoc can connect to;
- `/init /mnt/c/Windows/System32/cmd.exe /c ver` fails;
- copying any `.exe` into /home/brokeoc and running it, and running it as `/init /home/brokeoc/x.exe`, both fail;
- `mount | grep binfmt_misc` and `cat /proc/sys/fs/binfmt_misc/status` are recorded;
- `sysctl dev.tty.legacy_tiocsti` is recorded.

State in SPEC R1d: if any of these succeed, R1 stays OPEN on WSL.

## Should fix (not blocking)
- **S1. Serve stderr is a new cross-user channel into the operator’s terminal.**
  - `app/opencode/server.py:224-228` (`_stderr_detail`) and the log at `:379-380` write brokeoc-controlled bytes to the daemon log without stripping control characters, e.g. ESC/OSC 52 clipboard writes into Windows Terminal. `_detail` (line 230) does the same.
  - Reuse `_stderr_tail`-style control-character stripping (os_user.py:295-298) in `_stderr_detail`.
  - The executor’s Discord path already goes through `safe_text`.
- **S2. Mutation-time hangs (tester note).** I am not requiring this now:
  - unmutated code passes in 122 s;
  - the e2e and server tests use bounded `wait_for` (20 s) and `grab_port` (10 s);
  - the outer `timeout 400` keeps CI bounded.

  Still, add `pytest-timeout` as a dev dependency with `timeout = 120` in the pytest config, so a regression fails per test instead of hanging the file. Do it in the next round.
- **S3.** A FIFO or slow `auth.json` makes `SystemHostProbe.run` time out and kill sudo only (os_user.py:221-225). The `head` started as brokeoc stays blocked until the next sweep: one leaked process per task.
  - This is fail-closed ("unreadable"), so it is only the agent DoS-ing itself.
  - Optional: `_pkill("-KILL", "-s", str(proc.pid))` on probe timeout, run from `SudoLauncher`.
- **S4.** Per-run processes that the agent `setsid`s away from the `opencode run` session survive `kill_tree` (`pkill -s`) until the next serve reap or daemon stop. N1 is closed for serve restarts and stop, and narrowed for runs. Document this in L3.

## Bypass hunt (all OK unless listed above)
- **Sudoers scope.** The runas list is only `(brokeoc)`. There is no `ALL`, no root and no group runas, so `-g` is limited to brokeoc’s own group.
  - The names and path are regex-checked, with no sudoers metacharacters (comma, colon, equals, star, backslash, space).
  - The commands are opencode, pkill, test and head, all as brokeoc. Even arbitrary arguments (pkill `-F`, head on any path, opencode subcommands that exec programs) only act with brokeoc’s rights. The daemon user already has more than that.
  - The agent (brokeoc) has no sudo rights at all, and is checked not to be in sudo/adm/docker/lxd/disk/shadow/wheel/admin/root.
  - If `opencode.command[0]` were an interpreter or `/usr/bin/sudo`, that would still only run as brokeoc.
  - No path from this rule leads to root or to the daemon user.
- **Argv and option injection.** The command is `[/usr/bin/sudo, -n, -u, name, --, abs_exe, ...]`. The prompt is one element; a leading `-` is space-prefixed by `build_argv`, and the audit tests `-u root -- /bin/sh` inside the prompt. No shell anywhere, and `SystemHostProbe.run` uses exec with a fixed PATH.
- **env_reset / env_keep.**
  - The env sent to sudo is already only {PATH, LANG, the three OPENCODE_* values}, so the env_keep list is defence in depth. `=` replaces the list rather than appending.
  - Neither the password nor the token is in argv, so they do not reach the sudo log. The prompt does (R1f).
- **Signals.** brokeoc cannot signal sudo or the daemon (uid mismatch).
  - `killpg` from the daemon only reaches the sudo pgid.
  - The pkill selectors are only `-s <launch pid>` and `-U brokeoc`, and they run as brokeoc, so a reused pid cannot hit the daemon’s processes.
- **Self-check, fail-closed.**
  - It runs in the documented order.
  - rc None or any other rc fails.
  - The home `-r`/`-x` probes act as a catch-all for everything under the daemon home: DB, config, logs, `~/.aws`, `~/.ssh`.
  - `/proc/<pid>/environ` and `/mnt/c` are probed.
  - It runs before serve, and a failure gives exit 3.
  - It never sweeps.
  - `--once` never starts the launcher.
- **N9** (executor.py:381-386): after the INVALID and project-dir checks, before the spawn. Correct.
- **N10** (server.py:172-175): the instruction comes first, capped at 300 and still starting with "managed OpenCode server is down". Correct.
- **N1** (server.py:255-257): sweeps on every reap, plus launcher start/stop. Services stop in the order manager, launcher, gateway. Correct.

## Follow-ups (carried forward, current status)
1. **R1: NARROWED, not yet closed.**
   - The code implementation is correct: brokeoc cannot read the daemon home, secrets, DB or `/proc` environ, and interop fails closed. The R1a-R1f residuals are documented.
   - It becomes RESOLVED once Required 1-4 are done and T082 L1-L11 (plus the new L3/L4/L7 items) pass live.
2. **N1: RESOLVED** for serve restart and daemon stop. Runs are narrowed (S4).
3. **N4:** now R1b (documented).
4. **N9: RESOLVED.**
5. **N10: RESOLVED** (message only; no re-arm, out of scope).
6. **N2:** moot in sudo mode (passthrough must be empty). Still open, low, for mode none.
7. **N3, N5, N6, N7 (open, low):** unchanged.
8. **F1:** fixes 1 and 3 SUPERSEDED, fix 2 DONE; fix 4: command/project provider DONE, alias/baseURL SUPERSEDED.
9. **Resolved earlier:** F2, F3, F4, F7, F10, F17, F18, F19, N8.
10. **Superseded earlier:** F11, F20-F22.
11. **F5, F6, F8, F9, F12-F16 (open):** unchanged.
12. **F23 (open, in T082):** check whether GET /config echoes the gateway apiKey and can serve as a serve identity check; confirm the merge order of enabled_providers.
13. **F24:** keys SUPERSEDED. Plugin code execution is now contained to brokeoc, but see Required 1: agent-written plugins load when the user runs opencode as brokeoc from a terminal.
14. **Known gap (open):** a session cancelled before its first event is not aborted server-side.
15. **Deferred (open):**
    - approvals T060/T061;
    - start scripts T101/T102;
    - live E2E T082 (now including L1-L11);
    - B4;
    - T044;
    - concurrent tasks.
16. **Slice-2 follow-ups and the strict xfail (open):** unchanged.
17. **New, open:** Required 1-4 above.
18. **New, open:** S1-S4 above.
19. **New residuals, documentation only:**
    - the WSLg X11 socket (`/tmp/.X11-unix`) lets brokeoc reach Linux GUI apps the user runs;
    - services listening on the Windows host or localhost (mirrored networking, a Docker Desktop TCP API) are outside the OS-user boundary.
