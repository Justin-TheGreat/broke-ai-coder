# Spec: run OpenCode as a separate Linux user (T072, closes R1) + N9 + N10 (branch feat/opencode-user)

Goal: `opencode serve` and every `opencode run --attach` child run as a dedicated unprivileged
Linux user (default `brokeoc`) through `sudo -n -u brokeoc -- <exact argv>`. That user has its own
HOME and cannot read the daemon user's home, the secrets file, or `/proc/<daemon pid>/environ`.
The daemon never needs root. It keeps the keys, and OpenCode reaches models only through the
gateway.

Run everything with `.venv/Scripts/python` (`-m pytest -q`, `-m ruff check .`,
`-m ruff format --check .`, `-m app --check --config config.example.yaml`). Tests make no real
network calls and never run real sudo.

## OPEN QUESTION — RESOLVED by the user on 2026-10-04: implement the default (disable interop) exactly as written below.

**Q1. WSL interop and `/mnt/c` must be closed, or R1 stays open on WSL.**
- With WSL interop on, ANY Linux user (including `brokeoc`) can run a Windows `.exe` as the
  logged-in Windows account. It can also download one into its own home and run it. That
  Windows process can read `\\wsl$\...\home\<daemon user>\...\secrets.env` and every Windows
  file.
- With the default DrvFs mount, `/mnt/c` is world-readable.
- **Default:**
  - The setup guide adds `[interop] enabled=false`, `appendWindowsPath=false` and
    `[automount] options="umask=077"` to `/etc/wsl.conf`.
  - The self-check FAILS CLOSED while any `WSLInterop*` binfmt entry is enabled.
  - `/mnt/c` is in the default `protected_paths`, so the agent must not be able to read it.
- **Cost to the user:** no launching Windows programs from WSL shells (`code .`,
  `explorer.exe`, `clip.exe`, `powershell.exe`). The user must confirm. Everything else in this
  spec is settled.

## Design decisions (settled)

1. **Launch mechanism: sudo with a NOPASSWD rule whose runas is restricted to `brokeoc`.**
   - Every command runs as `brokeoc`, never root. Running a command as the LESS-privileged user
     cannot raise the daemon's privileges, so the rule is safe by construction.
   - Commands are exact absolute paths with any arguments: the opencode binary, plus
     `/usr/bin/pkill`, `/usr/bin/test` and `/usr/bin/head`. Arguments cannot be pinned because
     the prompt is an argument.
   - argv goes to sudo after `--`, with no shell anywhere. sudo itself runs no shell.
   - Rejected alternatives:
     - `runuser`/`setpriv` need root.
     - A systemd unit (`User=brokeoc`) needs polkit or sudo for `systemctl` anyway. It would also
       force the per-run password and token onto disk as env files, and it does not cover the
       per-task `opencode run`.
     - ssh to localhost needs sshd and key management.
2. **Environment.**
   - sudoers sets `env_reset` and `env_keep = "<exact list>"` (replace, not append) in a
     `Defaults>brokeoc` entry.
   - The env given to the sudo process is only `PATH=/usr/bin:/bin`, `LANG` (if present), and
     the allowlisted `OPENCODE_*` extras.
   - The daemon's HOME/XDG_*/keys never reach sudo. sudo sets HOME/USER/LOGNAME/SHELL/PATH from
     brokeoc's passwd entry and secure_path.
   - `OPENCODE_CONFIG_CONTENT` (gateway token, not a provider key) and the serve password are
     acceptable for the agent to hold.
3. **Killing processes across users.**
   - The daemon cannot signal brokeoc processes, so it uses `sudo -n -u brokeoc -- /usr/bin/pkill`.
   - Per launch, it targets the session id: the launcher pid, because `spawn_kwargs()` uses
     `start_new_session=True`.
   - On every serve reap/stop and at startup it runs a sweep, `pkill -KILL -U brokeoc`.
     `brokeoc` runs nothing else. This closes **N1** (detached agent processes outliving serve).
4. **Filesystem.**
   - Daemon home is 700 and the secrets file is 600.
   - Shared group `brokeai` = {daemon user, brokeoc}.
   - `/workspace` is `daemon:brokeai 2770` with default ACL `g:brokeai:rwX`.
   - The serve cwd is `/var/lib/broke-ai-coder/opencode-server` (`brokeoc:brokeai 2770`).
   - The opencode binary is root-owned at `/usr/local/bin/opencode`.
   - brokeoc's home is 700. The daemon never reads it directly: the auth.json check runs
     `test`/`head` AS brokeoc.
5. **/proc.**
   - `/proc/<pid>/environ` is mode 0400. Reading it also requires PTRACE_MODE_READ (same uid or
     CAP_SYS_PTRACE), so another unprivileged uid cannot read it.
   - hidepid is NOT used: WSL `/proc` remount is awkward, and the daemon's cmdline holds no
     secrets.
   - The self-check probes it anyway.
6. **TIOCSTI.**
   - All sudo children are spawned with `start_new_session=True` (no controlling tty),
     `stdin=DEVNULL` and the default `close_fds=True`, so no tty fd is reachable.
   - The sudoers file therefore sets `!use_pty`. That keeps stdin/stdout as our pipes and
     /dev/null, which the executor relies on (B9).
7. **Default mode.**
   - `opencode.os_user.mode` defaults to `"sudo"` (fail closed).
   - `"none"` = today's behaviour (same user), for tests, Windows and dev. It is loudly warned
     as unsupported for production.
   - The test config sets `"none"`.
8. **Self-check.**
   - Runs as a `RuntimeService.start()` placed before the managed serve. Failure raises
     `StartupError`, so the daemon exits 3 and runs no tasks.
   - Also available as `python -m app --check-os-user`.

## Recorded assumptions (put in the `app/opencode/os_user.py` docstring; verify live, see T082 L1-L11)

- S1. sudo relays SIGTERM it receives to the command. It cannot relay SIGKILL.
- S2. Without a pty, the command stays in sudo's session, so sid == launcher pid. If sudo instead
  execs directly, the pid is the command's and sid is the same.
- S3. `Defaults>brokeoc env_keep = "..."` applies to commands run as brokeoc.
- S4. Under env_reset, Ubuntu sudo sets HOME/USER/LOGNAME/SHELL from the target passwd entry and
  PATH from secure_path. sudo keeps the caller's cwd.
- S5. With XDG_DATA_HOME unset for brokeoc, OpenCode's auth store is
  `<brokeoc home>/.local/share/opencode/auth.json` (G6).
- P1. /proc environ: see decision 5.
- P2. TIOCSTI: see decision 6.
- W1. WSL interop binfmt entries are named `WSLInterop` and/or `WSLInterop-late` under
  `/proc/sys/fs/binfmt_misc/`. Their first line is `enabled` or `disabled`.

## Files

### Create `app/opencode/os_user.py`

Copy patterns from:
- `app/opencode/process.py`: subprocess spawn, never-raise kill, `logger.warning(type(e).__name__)`.
- `app/runtime/interfaces.py`: Protocol style.

Rules:
- No top-level `pwd`/`grp` import. Import them lazily inside `SystemHostProbe`, so the module
  imports on Windows.
- Signals are passed as names ("TERM"/"KILL"), never `signal.SIGKILL` at module level.

```python
SUDO = "/usr/bin/sudo"; PKILL = "/usr/bin/pkill"; TEST = "/usr/bin/test"; HEAD = "/usr/bin/head"
SUDO_CHILD_PATH = "/usr/bin:/bin"
KEEP_ENV: tuple[str, ...] = ("LANG", "OPENCODE_CONFIG_CONTENT", "OPENCODE_SERVER_PASSWORD",
                             "OPENCODE_SERVER_USERNAME")
SUDOERS_NAME_RE = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")
SUDOERS_PATH_RE = re.compile(r"^/[A-Za-z0-9._+/-]+$")
PRIVILEGED_GROUPS = frozenset({"root", "sudo", "admin", "wheel", "adm", "docker", "lxd", "disk", "shadow"})
_PROBE_TIMEOUT_S = 10.0; _VERSION_TIMEOUT_S = 30.0; _AUTH_MAX_BYTES = 1_048_576
ISOLATION_FAILED = "OpenCode user isolation (opencode.os_user) check failed: "

@dataclass(frozen=True, slots=True)
class UserInfo: name: str; uid: int; gid: int; home: str

@dataclass(frozen=True, slots=True)
class ProbeResult: returncode: int | None; stdout: bytes = b""; stderr: bytes = b""
# returncode None = timeout or OSError

class HostProbe(Protocol):
    def platform(self) -> str: ...                     # sys.platform
    def euid(self) -> int: ...
    def pid(self) -> int: ...
    def user_by_uid(self, uid: int) -> UserInfo | None: ...
    def user_by_name(self, name: str) -> UserInfo | None: ...
    def group_names(self, user: UserInfo) -> frozenset[str]: ...   # os.getgrouplist + grp
    def interop_enabled(self) -> bool: ...  # any /proc/sys/fs/binfmt_misc/WSLInterop* whose first
                                            # line is "enabled"; unreadable entry -> True
    def is_dir(self, path: str) -> bool: ...
    def killpg(self, pgid: int, sig: str) -> None: ...  # quiet; sig in {"TERM","KILL"}
    async def run(self, argv: Sequence[str], *, timeout_s: float) -> ProbeResult: ...

class SystemHostProbe:  # real implementation
```

`SystemHostProbe.run` rules:
- Call `asyncio.create_subprocess_exec(*argv, stdin=DEVNULL, stdout=PIPE, stderr=PIPE, env={"PATH": SUDO_CHILD_PATH}, start_new_session=True)`.
- Then `communicate()` under `wait_for(timeout_s)`.
- On timeout: `proc.kill()` (suppress errors), wait, and return `ProbeResult(None)`.
- On OSError: return `ProbeResult(None)`.

```python
class OpenCodeLauncher(Protocol):  # also a RuntimeService
    isolated: bool

    def executable(self, command0: str) -> str: ...  # raises StartupError
    def command(self, argv: Sequence[str]) -> list[str]: ...
    def environment(
        self, environ: Mapping[str, str], *, passthrough: Sequence[str], extra: Mapping[str, str]
    ) -> dict[str, str]: ...
    async def kill_tree(self, proc: asyncio.subprocess.Process, *, grace_s: float) -> None: ...
    async def sweep(self) -> None: ...
    async def auth_store_problem(self) -> str | None: ...
    async def start(self) -> None: ...
    async def stop(self) -> None: ...
```

**`DirectLauncher(environ: Mapping[str, str] | None = None)`** (`isolated = False`) keeps today's
behaviour exactly:
- `executable` = `resolve_executable`.
- `command` = `list(argv)`.
- `environment` = `build_opencode_env(...)`.
- `kill_tree` = `kill_process_tree`.
- `sweep`/`stop` do nothing.
- `auth_store_problem` = `await asyncio.to_thread(auth_store_problem, environ)`.
- `start()` logs ONE warning: `"opencode.os_user.mode is 'none': OpenCode runs as the daemon user and can read its secrets (residual risk R1); unsupported for production"`.

**`SudoLauncher(config: AppConfig, *, probe: HostProbe)`** (`isolated = True`). `name` =
`config.opencode.os_user.name`. Methods:

- `executable(c0)`: return `c0` if `PurePosixPath(c0).is_absolute()`. Otherwise raise
  `StartupError("opencode.command[0] must be an absolute path when opencode.os_user.mode is sudo")`.
- `command(argv)`: `[SUDO, "-n", "-u", name, "--", *argv]`.
- `environment(environ, *, passthrough, extra)`:
  - Return `{"PATH": SUDO_CHILD_PATH}`, plus `LANG` if it is in `environ`, plus the extras whose
    key is in `KEEP_ENV`. Other extras are dropped, because sudo would drop them anyway.
  - `passthrough` is ignored: the self-check rejects a non-empty passthrough.
- `_pkill(*args)`:
  - `await probe.run(self.command([PKILL, *args]), timeout_s=_PROBE_TIMEOUT_S)`.
  - Never raises. Log a warning if the return code is not in (0, 1).
- `kill_tree(proc, *, grace_s)`: never raises; wrap everything in `try/except Exception` and log.
  `pid = proc.pid` (always > 0).
  - If `proc.returncode is not None`: `_pkill("-KILL", "-s", str(pid))` and return.
  - Otherwise, in this order:
    1. `probe.killpg(pid, "TERM")`
    2. `_pkill("-TERM", "-s", str(pid))`
    3. wait up to `grace_s` for `proc.wait()`
    4. `_pkill("-KILL", "-s", str(pid))`
    5. `probe.killpg(pid, "KILL")`
    6. if still running: `proc.kill()` (suppress errors) and wait `grace_s`
- `sweep()`: `_pkill("-KILL", "-U", name)`. If the return code is 0, log
  `"killed leftover processes of OpenCode user %s"` at info level.
- `auth_store_problem()`: never raises.
  - `path = f"{user.home}/.local/share/opencode/auth.json"`, where `user = probe.user_by_name(name)`.
    If the user is None, return `"OpenCode user missing: <name>"`.
  - Run `r = run(command([TEST, "-e", path]))`.
    - `r.returncode == 1`: return None.
    - Any return code other than 0 or 1: unreadable.
  - Run `h = run(command([HEAD, "-c", str(_AUTH_MAX_BYTES + 1), "--", path]))`.
    - `h.returncode != 0` or `len(h.stdout) > _AUTH_MAX_BYTES`: unreadable.
  - Decode `h.stdout` as utf-8-sig. A decode error means unreadable.
  - Compute keys with `credential_providers_from_text`, then return `credential_problem(keys, path)`.
  - "Unreadable" means `credential_problem(None, path)`, i.e. the existing
    "OpenCode credential store unreadable: <path>" message.
- `self_check() -> None`:
  - Raises `StartupError(ISOLATION_FAILED + reason)` at the FIRST failing check, in this order.
    Messages are exact; `{name}` and `{path}` are substituted.
  - It never sweeps and never starts serve.

  1. `not probe.platform().startswith("linux")`: `"mode 'sudo' needs Linux (WSL); set opencode.os_user.mode: none for tests/dev (unsupported for production)"`
  2. `probe.euid() == 0`: `"the daemon must not run as root"`
  3. `daemon = probe.user_by_uid(euid)` is None: `"cannot look up the daemon user"`
  4. `oc = probe.user_by_name(name)` is None: `"user '{name}' does not exist; create it (docs/SETUP-discord-opencode.md, Part C6)"`
  5. `oc.uid in (0, daemon.uid)`: `"user '{name}' must differ from the daemon user and must not be root"`
  6. `bad = probe.group_names(oc) & PRIVILEGED_GROUPS`: `"user '{name}' is in privileged group(s) {', '.join(sorted(bad))}; remove it (sudo gpasswd -d {name} <group>)"`
  7. command[0] is not absolute: `"opencode.command[0] must be an absolute path (e.g. /usr/local/bin/opencode)"`. If it is under the daemon home: `"opencode.command[0] must not be inside the daemon user's home"`.
  8. `config.opencode.env_passthrough` is non-empty: `"opencode.env_passthrough must be empty (sudo resets the environment)"`
  9. Only if `managed_server`: `server_directory` is not absolute, or is under the daemon home: `"opencode.server_directory must be an absolute path outside the daemon user's home (e.g. /var/lib/broke-ai-coder/opencode-server)"`
  10. `probe.interop_enabled()`: `"WSL interop is enabled: the OpenCode user could start Windows programs as your Windows account; set [interop] enabled=false in /etc/wsl.conf, then run wsl --shutdown"`
  11. Run `run(command([exe, *command[1:], "--version"]), timeout_s=_VERSION_TIMEOUT_S)`. If the return code is not 0: `` "`sudo -n -u {name} -- {exe} --version` failed ({detail}); install the sudoers file from `python -m app --print-sudoers`" ``.
      - `detail` is `"timed out"` when the return code is None.
      - Otherwise `detail` is `f"exit {rc}"`, plus `": " + tail` when the stderr tail is non-empty.
      - The stderr tail is the last 200 chars, decoded with `errors="replace"`, control characters
        removed and whitespace stripped.
  12. Access probes, each `run(command([TEST, flag, path]))`:
      - Targets, in order:
        - `(daemon.home, "-r")`
        - `(daemon.home, "-x")`
        - each `protected_paths` entry with a leading `~/` replaced by `daemon.home + "/"`, as `"-r"`
        - `os.path.abspath(config.database.path)` as `"-r"`
        - `f"/proc/{probe.pid()}/environ"` as `"-r"`
      - rc 1 passes.
      - rc 0: `"user '{name}' can access {path} ({flag}); fix permissions (Part C6: chmod 700 your home, chmod 600 the secrets file, automount umask=077 in /etc/wsl.conf)"`
      - Any other rc (including None): `"access probe failed for {path}"`.
      - Use a POSIX join; never `os.path.expanduser`.
  13. Only if `managed_server`: `test -w server_directory` and `test -x server_directory` must both return 0. Otherwise: `"user '{name}' cannot write opencode.server_directory {path}"`.
  14. Not fatal. For each directory in `[working_directory, *projects.values()]` where
      `probe.is_dir(d)`: if `test -w` or `test -x` returns non-zero, log the warning
      `"OpenCode user %s cannot write project directory %s; tasks there cannot edit files"`.
  - "Under the daemon home" means `path == home` or `path.startswith(home.rstrip("/") + "/")`,
    using POSIX string comparison.
- `start()`:
  1. `await self_check()`.
  2. `await sweep()`, to clear processes left by a previous daemon.
  3. Log info `"OpenCode runs as %s (uid %d) via sudo"`.
- `stop()`: `await sweep()`.

**`render_sudoers(daemon_user: str, oc_user: str, opencode_path: str) -> str`**:
- Raise `ValueError` with a short reason if a name fails `SUDOERS_NAME_RE`, a name is
  `root`/`ALL`, the two names are equal, or the path fails `SUDOERS_PATH_RE`.
- Output, exactly, ending with a newline:

```
# /etc/sudoers.d/broke-ai-coder (generated by `python -m app --print-sudoers`)
# Lets {daemon} run OpenCode and three helper tools as {oc} only - never as root - without a
# password. {oc} is the less privileged user, so this grants {daemon} nothing it lacks.
# Validate with `visudo -cf` before installing (docs/SETUP-discord-opencode.md, Part C6).
Defaults>{oc} env_reset
Defaults>{oc} env_keep = "LANG OPENCODE_CONFIG_CONTENT OPENCODE_SERVER_PASSWORD OPENCODE_SERVER_USERNAME"
Defaults>{oc} !use_pty
Defaults>{oc} !requiretty
{daemon} ALL=({oc}) NOPASSWD: {opencode}, /usr/bin/pkill, /usr/bin/test, /usr/bin/head
```

- Build the env_keep list from `KEEP_ENV` so the two stay in sync.

**`build_launcher(config, *, environ=None, probe: HostProbe | None = None) -> OpenCodeLauncher`**:
mode `"none"` returns `DirectLauncher(environ)`; mode `"sudo"` returns
`SudoLauncher(config, probe=probe or SystemHostProbe())`.

### Modify `app/config/models.py` (copy the `GatewayConfig` style)

```python
class OsUserConfig(BaseModel):
    model_config = _CFG
    mode: Literal["sudo", "none"] = "sudo"
    name: str = Field("brokeoc", pattern=r"^[a-z_][a-z0-9_-]{0,31}$")
    protected_paths: list[str] = Field(
        default_factory=lambda: ["~/.config/broke-ai-coder/secrets.env", "/mnt/c"]
    )
```

Validators:
- `name` must not be `root` (pydantic `ValueError`).
- Each `protected_paths` entry is stripped, non-empty, has no NUL, and starts with `/` or `~/`.

Add to `OpenCodeConfig`: `os_user: OsUserConfig = Field(default_factory=OsUserConfig)`.

Do NOT add OS-dependent validation to pydantic. Path and passthrough rules live only in
`self_check`.

### Modify `app/opencode/auth_store.py`

Refactor without changing behaviour. Add:
- `credential_providers_from_text(text: str) -> list[str] | None`: the JSON-object key logic
  that exists today, with sanitizing and the `_MAX_KEYS` cap.
- `credential_problem(keys: list[str] | None, path: str | Path) -> str | None`: today's two
  messages.

`stored_credential_providers` and `auth_store_problem` must use them. Add an S5 note to the docstring.

### Modify `app/opencode/config_check.py`

`check_server_config(..., environ=None, auth_problem: Callable[[], Awaitable[str | None]] | None = None)`.
When `auth_problem` is given, use `await auth_problem()`. Otherwise keep the current
`to_thread(auth_store_problem, environ)`.

### Modify `app/opencode/process.py`

Rename `_wait` to `wait_quiet`, which is public so `os_user.py` can reuse it. Update the internal
callers. Nothing else changes.

### Modify `app/opencode/server.py`

- `OpenCodeServerManager.__init__(..., launcher: OpenCodeLauncher | None = None)`. The default is
  `DirectLauncher(environ)`.
- `start()`:
  - `problem = await self._launcher.auth_store_problem()`.
  - `self._executable = self._launcher.executable(self._oc.command[0])`.
- `_spawn()`:
  - `argv = self._launcher.command([...today's serve argv...])`.
  - `env = self._launcher.environment(os.environ if self._environ is None else self._environ, passthrough=..., extra={...same three...})`.
- `_reap()`:
  - Replace `kill_process_tree` with `await self._launcher.kill_tree(proc, grace_s=...)`.
  - After the drain is handled, ALWAYS `await self._launcher.sweep()`, guarded by
    `contextlib.suppress(Exception)`.
- `_wait_healthy`: when `self._launcher.isolated`, append
  `" (or the sudoers env_keep line is missing, so serve got no password)"` to the
  "does not enforce the password" `_PortInUse` message.
- **N10**:
  - `unavailable_reason()` for a fatal error returns
    `("managed OpenCode server is down and will not restart by itself; fix the cause, then restart the daemon: " + fatal)[:_DETAIL_CAP]`.
  - The fatal log line becomes
    `"opencode serve cannot restart; giving up until the daemon is restarted: %s"`.
  - The "restarting" text stays unchanged.
- Docstring: replace the R1 paragraph with the os_user model and point to the os_user.py
  assumptions.

### Modify `app/opencode/executor.py`

- `__init__(..., launcher: OpenCodeLauncher | None = None)`. The default is `DirectLauncher(environ)`.
- `check_server_config` passes `auth_problem=self._launcher.auth_store_problem`.
- `_child_env()` uses `self._launcher.environment(...)` with the same extras as today.
- In `run()`:
  - `executable = self._launcher.executable(...)` replaces `resolve_executable`. `StartupError`
    maps to `_unavailable(str(e))`.
  - **N9**: right after the project-directory check and before the spawn: if
    `self._unavailable_reason is not None` and `(reason := self._unavailable_reason()) is not None`,
    return `_unavailable(reason)`. Nothing is spawned.
  - `argv = self._launcher.command(self.build_argv(...))`. `build_argv` itself is unchanged.
  - Both kill calls become `self._launcher.kill_tree(proc, grace_s=grace)`. The shielded one
    stays shielded.
- Add B14 to the docstring: in sudo mode the process runs as `opencode.os_user.name`, and N9
  holds.

### Modify `app/runtime/wiring.py`

- `launcher = build_launcher(config, environ=environ)` is built once and passed to BOTH the
  manager and the executor.
- `Runtime` gains `launcher: OpenCodeLauncher`, as the last field.
- `services = [] if once else [gateway, launcher]`, then `append(manager)` when a manager exists.
- The launcher's `start` runs the self-check before serve, and its `stop` sweeps after the
  manager stops (services stop in reverse order).

### Modify `app/cli.py`

Add two options to the mutually exclusive group.

**`--print-sudoers`**
- Load the config (exit 2 on `ConfigError`).
- If `os.name != "posix"`: print `"run --print-sudoers inside WSL as the daemon user"` to stderr
  and exit 2.
- daemon user = `SystemHostProbe().user_by_uid(os.geteuid()).name`.
- Print `render_sudoers(daemon, cfg.opencode.os_user.name, cfg.opencode.command[0])` to stdout.
  Print nothing else on stdout.
- `ValueError`: print it to stderr and exit 2.

**`--check-os-user`**
- mode `none`: print
  `"opencode.os_user.mode is none: OpenCode would run as the daemon user (unsupported for production)"`
  to stderr and exit 3.
- Otherwise run `asyncio.run(build_launcher(cfg).self_check())`.
  - On success, print `"os_user OK: OpenCode runs as <name>; isolation checks passed"` and exit 0.
  - On `StartupError`, print it to stderr and exit 3.
- It must NOT sweep: a running daemon's serve must survive.

### Modify `config.example.yaml` (under `opencode:`)

- `command: [/usr/local/bin/opencode]`. Comment: absolute path required with os_user sudo;
  copied in Part D1.
- `server_directory: /var/lib/broke-ai-coder/opencode-server`. Comment: must be writable by
  os_user, outside your home.
- Add:

```yaml
  os_user:
    mode: sudo          # none = OpenCode runs as the daemon user: tests/Windows dev only, unsupported for production (R1)
    name: brokeoc       # dedicated user, see docs/SETUP-discord-opencode.md Part C6
    protected_paths: ["~/.config/broke-ai-coder/secrets.env", "/mnt/c"]  # must be unreadable by os_user
```

- `env_passthrough: []` comment: must stay empty with os_user sudo.

### Tests

- `tests/conftest.py`: `_base_config_dict()` gains `"opencode": {"os_user": {"mode": "none"}}`.
  `make_config`'s dict-merge keeps it when tests override other `opencode` keys.
- Fix any other test that builds a runtime from a config without `mode: none` by adding it. Do
  NOT weaken assertions.
- `test_wiring.py`: service lists become `[rt.gateway, rt.launcher, rt.server_manager]` and
  `[rt.gateway, rt.launcher]`.

New `tests/test_opencode_os_user.py`:
- Use a `FakeHostProbe` that records `run` argv lists and `killpg` calls, and returns programmed
  `ProbeResult`s keyed by argv (default rc 1 for `test` probes, 0 otherwise).
- Use a fake proc object with `pid`, `returncode`, `wait()` and `kill()`.

1. `render_sudoers("justin", "brokeoc", "/usr/local/bin/opencode")` equals the exact text.
   Its env_keep matches `KEEP_ENV`. `ValueError` for: `"ALL"`, `"root"`, `"Bad"`, `"a b"`,
   `"x,y"`, equal names, a relative path, and paths containing space, `,`, `*`, `:`, `=`, `\`.
2. `SudoLauncher.command`:
   - Output starts with `[SUDO, "-n", "-u", "brokeoc", "--"]`.
   - A prompt containing `; $(rm -rf ~) "q" 'x'` and newlines stays one unchanged element.
   - A prompt built by `build_argv` that starts with `" -x"` comes after `--`.
3. `SudoLauncher.environment`:
   - Input environ has PATH, HOME, XDG_DATA_HOME, LANG, OPENROUTER_API_KEY, AWS_SECRET_ACCESS_KEY
     and DISCORD_BOT_TOKEN.
   - Extras are the three OPENCODE_* plus `FOO`.
   - Result keys are exactly {PATH, LANG, the three}, and PATH == "/usr/bin:/bin".
4. `self_check`:
   - Happy path passes, and the recorded probe argv sequence matches spec order 11 → 13.
   - Each failure 1-13 raises `StartupError`, whose message starts with `ISOLATION_FAILED` and
     contains the key phrase. Cases:
     - the stderr text `"a password is required"` appears in the 11 message;
     - rc None gives "timed out";
     - rc 2 on an access probe gives "access probe failed";
     - `~/x` expands with the probe's daemon home.
   - With `managed_server: false`, checks 9 and 13 are skipped.
   - A non-writable project directory only logs a warning (use caplog), and the check passes.
   - `self_check` never calls pkill.
5. `start()`: self_check, then exactly one `pkill -KILL -U brokeoc`. When self_check fails,
   `start()` raises and no pkill is called. `stop()` sweeps.
6. `kill_tree`:
   - A running proc that exits during the grace period gives the exact sequence (killpg TERM,
     pkill -TERM -s pid, pkill -KILL -s pid, killpg KILL).
   - An exited proc gives only `pkill -KILL -s pid`.
   - A probe that raises or returns rc None/3 never propagates.
7. `auth_store_problem`:
   - Missing file (rc 1) gives None.
   - `{}` gives None.
   - `{"openai": {"key": "sk-x"}}` gives the stored-credentials message, which names `openai`
     and never contains `sk-x`.
   - head rc 1, invalid JSON, oversize output and test rc None each give "unreadable".
   - The head argv is `[..., HEAD, "-c", "1048577", "--", "/home/brokeoc/.local/share/opencode/auth.json"]`.
8. `DirectLauncher` matches today's helpers. `start()` logs "unsupported for production".
   `build_launcher` selects by mode.

Additions to existing files:
- `test_config.py`: os_user defaults; the name `root`/`ALL`/`Bad` is rejected; a relative
  protected path is rejected; a bad mode is rejected.
- `test_opencode_server.py`: use a recording `DirectLauncher` subclass so the fake serve still
  runs. Check that:
  - serve argv/env come from the launcher;
  - `_reap` calls `kill_tree` and then `sweep`;
  - `start` uses `launcher.auth_store_problem` (a problem raises `StartupError`);
  - the "does not enforce the password" message gains the env_keep hint only when `isolated`.
  - N10: after a fatal error, `unavailable_reason()` starts with "managed OpenCode server is
    down", contains "restart the daemon" and "already in use", and is ≤ 300 chars.
- `test_opencode_executor.py`:
  - N9: the reason is set, so `run()` returns `EXECUTOR_UNAVAILABLE` with `detail == reason`
    and `create_subprocess_exec` is never called (monkeypatch it to fail the test).
  - Reason None gives a normal spawn.
  - The spawned argv equals `launcher.command(build_argv(...))`.
  - A timeout calls `launcher.kill_tree`.
- `test_managed_crash_window_e2e.py` (copy its setup), N9 fallback test:
  - Attempt 1 ends with a fallback-eligible class (rate limit) and the manager becomes
    not-ready before attempt 2.
  - Attempt 2 returns `EXECUTOR_UNAVAILABLE` with no spawn.
  - The task is FAILED `OPENCODE_UNAVAILABLE` with 2 attempt rows, the second with status
    `EXECUTOR_UNAVAILABLE`.
  - A cooldown is recorded only for attempt 1.
  - The Discord text contains the reason.
  - Also update the fatal e2e test to assert "restart the daemon" in the Discord text.
- `test_bot_format.py`: N10 fatal detail text appears.
- `test_opencode_config_check.py`: the `auth_problem` callable is used when given.
- `test_cli.py`:
  - `--print-sudoers` (monkeypatch `os.name`/the user lookup) prints the exact text with exit 0;
    a relative command gives exit 2.
  - `--check-os-user` gives exit 3 in mode none, and exit 0 with a monkeypatched `self_check`.

### Docs

**`docs/SETUP-discord-opencode.md`**
- Overview tree: add `brokeoc` (OpenCode user, own home `/home/brokeoc`),
  `/usr/local/bin/opencode`, `/var/lib/broke-ai-coder/opencode-server`, and `/workspace`
  (group `brokeai`).
- C4: replace the R1 paragraph. R1 is closed by Part C6 (OpenCode runs as `brokeoc`, which
  cannot read your home, `secrets.env` or the daemon's `/proc`). Keep "never source from
  .bashrc". Link the remaining residuals.
- Replace C5 with group-shared /workspace:
  ```bash
  sudo apt install -y acl
  sudo groupadd brokeai && sudo usermod -aG brokeai "$USER"
  ```
  Then `/workspace`: `chown "$USER:brokeai"`, `chmod 2770`, `chgrp -R brokeai`,
  `find /workspace -type d -exec chmod g+s {} +`, `setfacl -R -m g:brokeai:rwX`,
  `setfacl -R -d -m g:brokeai:rwX`. Then re-login (`exit`; in PowerShell
  `wsl --terminate Ubuntu-24.04`) so that `id` shows brokeai. Never put the controller repo in
  /workspace.
- New C6 "OpenCode user (one-time)", in this order:
  1. `sudo useradd --create-home --home-dir /home/brokeoc --shell /bin/bash --user-group brokeoc`; `sudo usermod -aG brokeai brokeoc`; `sudo chmod 700 /home/brokeoc`; `id brokeoc` (must not list sudo/adm/docker/lxd).
  2. `chmod 700 "$HOME" ~/.config/broke-ai-coder`; `chmod 600 ~/.config/broke-ai-coder/secrets.env`.
  3. `sudo install -d -m 0755 -o root -g root /var/lib/broke-ai-coder`; `sudo install -d -m 2770 -o brokeoc -g brokeai /var/lib/broke-ai-coder/opencode-server`.
  4. `/etc/wsl.conf`: MERGE these into the file with `sudo nano` and keep `[boot] systemd=true`:
     `[interop] enabled=false`, `appendWindowsPath=false`, `[automount] options="umask=077"`.
     Then in PowerShell run `wsl --shutdown` and reopen. Explain the cost (Q1).
  5. Git for the agent: `sudo -u brokeoc git config --global user.name "broke-ai-coder agent"`, a user.email placeholder, and `safe.directory '*'`. brokeoc gets no GitHub credentials: push as yourself.
  6. Sudoers. Never use /tmp, because brokeoc can write there:
     ```bash
     tmp=$(mktemp ~/broke-ai-coder.sudoers.XXXXXX)
     .venv/bin/python -m app --print-sudoers --config config.yaml > "$tmp"
     sudo visudo -cf "$tmp"
     sudo install -m 0440 -o root -g root "$tmp" /etc/sudoers.d/broke-ai-coder
     rm "$tmp"
     sudo visudo -c
     ```
     The file name has no dot, so sudo reads it.
  7. Verify:
     - `sudo -k`
     - `sudo -n -u brokeoc -- /usr/local/bin/opencode --version` works with no password prompt.
     - `sudo -n true` must FAIL (no root).
     - `sudo -n -u brokeoc -- /usr/bin/test -r ~/.config/broke-ai-coder/secrets.env; echo $?` expects 1.
     - `.venv/bin/python -m app --check-os-user --config config.yaml` expects `os_user OK`.
  - Note: the daemon kills ALL brokeoc processes at start, at stop and on every serve restart.
    Do not use brokeoc for anything else.
  - Note: sudo logs each command line (including prompts) to the system journal/auth.log.
- D1: after installing, run
  `sudo install -m 0755 -o root -g root "$(command -v opencode)" /usr/local/bin/opencode`.
  Re-run it after every OpenCode upgrade. If you installed with npm, point `opencode.command` at
  the npm global binary path and regenerate sudoers.
- D2: run `sudo -n -u brokeoc -- /usr/local/bin/opencode auth list` and log out there. The
  checked file is now `/home/brokeoc/.local/share/opencode/auth.json`.
- D4: serve runs as brokeoc via sudo. Add `pgrep -u brokeoc -a` to inspect.
- Checklist:
  - Replace the "no ambient cloud credentials ... until T072" item with "brokeoc's home holds no
    cloud credentials".
  - Add "`--check-os-user` output".
- Troubleshooting rows:
  - `a password is required` → sudoers not installed or wrong user/path; regenerate.
  - `user 'brokeoc' can access <path>` → chmod per C6 / automount umask.
  - `WSL interop is enabled` → C6 step 4.
  - `cannot write opencode.server_directory` → C6 step 3.
  - `does not enforce the password ... env_keep` → reinstall the generated sudoers file.
  - `will not restart by itself ... restart the daemon` (N10) → stop whatever holds the port,
    then restart the daemon.
  - `mode 'sudo' needs Linux` → Windows dev: set `os_user.mode: none`.

**SPEC.md**
- §9: one paragraph. In production, serve and run execute as `opencode.os_user.name` via sudo,
  with an exact env allowlist. Startup isolation self-check fails closed (exit 3).
  `os_user.mode: none` is tests/dev only.
- §11: rewrite R1 as "Resolved by T072 when `opencode.os_user.mode: sudo`". Add the remaining
  residual sub-bullets:
  - **R1a.** Agent-written code in /workspace (git hooks, scripts, package files) runs as whoever
    executes it. Do not run git or tests there as the daemon user on unreviewed changes.
  - **R1b (was N4).** The agent holds the gateway token and serve password. It can call
    allowlisted free models and reconfigure or kill its own serve, but it has no keys. The
    per-task GET /config and auth checks fail closed, and the crafted-server identity gap is
    unchanged.
  - **R1c.** The daemon parses agent-writable project config files. Only key names are reported.
    A FIFO or huge file can stall or fail that check.
  - **R1d.** WSL interop and /mnt must stay closed (self-check enforced).
  - **R1e.** Mode `none` keeps the original exposure.
  - **R1f.** sudo logs prompts to the system journal.

**ARCHITECTURE.md**
- §11 diagram: mark OpenCode as running under OS user `brokeoc`, with the boundary line "no read
  access to daemon home/secrets/proc".
- R1 text: same as SPEC.
- §12 failure-modes rows:
  - isolation self-check fails → exit 3;
  - serve fatal (port taken) → tasks fail `OPENCODE_UNAVAILABLE` telling the user to restart the
    daemon;
  - stop/restart kills all brokeoc processes (N1).

**TASKS.md**
- T072: Status DONE, describing what is done. Acceptance: the agent cannot read the secrets file,
  daemon home or daemon /proc environ, and cannot write system dirs.
- T082: append "OS-user live checks". These are the only things that cannot be verified
  offline:
  - L1: `visudo -c` passes; `sudo -n -u brokeoc -- opencode --version` needs no password;
    `sudo -n true` fails.
  - L2: env_keep passes exactly the list:
    `OPENCODE_SERVER_USERNAME=x FOO=y sudo -n -u brokeoc -- /usr/bin/head -c 4096 /proc/self/environ | tr '\0' '\n'`
    shows the kept vars and no FOO. The daemon's serve passes the anon-401 check and the
    gateway-only GET /config.
  - L3 (S1/S2): during a task, `ps -o pid,ppid,sid,user,cmd -u brokeoc` shows opencode's sid ==
    the sudo pid. After /cancel and a run timeout, no `opencode run` is left. After daemon stop,
    `pgrep -u brokeoc` is empty (N1).
  - L4: as brokeoc (`sudo -u brokeoc bash`, admin password), these must all be denied:
    `cat ~<you>/.config/broke-ai-coder/secrets.env`, `cat /proc/<daemon pid>/environ`,
    `ls ~<you>`, `ls /mnt/c`, and running any `.exe`.
  - L5: agent-created files in /workspace are group brokeai with g+rw (default ACL). The daemon
    user's `git status` works, and the agent can commit (safe.directory).
  - L6: a planted entry in brokeoc's auth.json makes startup fail with the stored-credentials
    message, and per-task the task fails `OPENCODE_UNSAFE_CONFIG`.
  - L7 (W1): after the wsl.conf change and `wsl --shutdown`, no enabled `WSLInterop*` entry
    remains, including with systemd on.
  - L8: sudo journal entries contain prompts (documented).
  - L9: OpenCode's shell tool works with SHELL=/bin/bash from passwd. OpenCode's provider
    package download into /home/brokeoc/.cache works.
  - L10 (S4/B8): serve's cwd is server_directory and run's cwd is the project directory.
  - L11 (S5): the auth.json path under sudo env_reset is the default XDG path.

**README.md Notes**: add two bullets:
- Production (WSL) runs OpenCode as a separate Linux user through sudo
  (`opencode.os_user`, setup guide Part C6); `--print-sudoers` and `--check-os-user` help with
  setup.
- On Windows/tests set `opencode.os_user.mode: none`, which is unsupported for production.

## Edge cases the implementation must handle

- The prompt is never passed through a shell. sudo receives `--` before the absolute executable.
  The prompt is one argv element, and a leading `-` is already space-prefixed by `build_argv`.
- The env given to sudo never contains the daemon's HOME/XDG_*/keys/bot token. Extras outside
  `KEEP_ENV` are dropped.
- Every sudo invocation (launch, pkill, test, head, `--version`) uses `stdin=DEVNULL`,
  `start_new_session=True` and the default `close_fds`. That covers launches via
  `spawn_kwargs()` and probes via `SystemHostProbe.run`.
- `kill_tree`, `sweep` and `auth_store_problem` never raise. pkill rc 1 (no match) is normal.
  Timeouts are bounded (`_PROBE_TIMEOUT_S`).
- `self_check` never sweeps, so `--check-os-user` cannot kill a running daemon's serve. `start()`
  sweeps once after a successful check.
- `--once` builds the launcher but never starts it (no self-check, no sweep).
- `managed_server: false` with mode sudo: skip checks 9 and 13. Run clients still go through
  sudo.
- `~/` expansion uses the daemon's passwd home from the probe, never `os.path.expanduser`.
  Database paths are `os.path.abspath`'d.
- A missing protected path yields `test -r` rc 1, so it passes.
- brokeoc controls its own auth.json (it could be huge, a FIFO, a symlink or invalid). The read
  is bounded by `head -c` and the probe timeout; any anomaly means "unreadable", which fails
  closed. The read runs as brokeoc, so a symlink to daemon files is denied.
- `os_user.py` imports and its tests run on Windows: lazy `pwd`/`grp`, signal names as strings,
  POSIX paths via `PurePosixPath`/string ops.
- The N10 restart instruction comes before the fatal text, so the 300-char cap cannot cut it.
  The detail still starts with "managed OpenCode server is down" (the format.py gate).
- N9 runs after the existing invalid-request checks, so an invalid prompt still returns INVALID.
  The attempt row is finished as `EXECUTOR_UNAVAILABLE` and no cooldown is recorded (the
  existing orchestrator path).

## Out of scope (do not implement)

- N4 beyond the documentation (R1b).
- cgroups/systemd scopes.
- hidepid.
- Restricting `safe.directory`.
- Symlink/FIFO hardening of the project-config check (R1c).
- The re-arm path for fatal_error (N10 is message-only).
- N2, N3, N5-N7, F5-F16.
