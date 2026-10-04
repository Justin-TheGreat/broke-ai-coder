"""Run OpenCode as a dedicated unprivileged Linux user (T072, closes R1).

``opencode serve`` and every ``opencode run --attach`` child are started through
``sudo -n -u <name> -- <exact argv>`` (``opencode.os_user``). That user has its own HOME and
cannot read the daemon user's home, the secrets file or ``/proc/<daemon pid>/environ``. The
daemon never needs root: the sudoers rule only lets the daemon user run four absolute-path
commands AS the less privileged OpenCode user (never as root), so it grants nothing the daemon
does not already have. There is no shell anywhere: sudo receives argv after ``--``.

Mode ``none`` keeps the old behaviour (same user) for tests, Windows and dev; it is unsupported
for production. Mode ``sudo`` is the default and fails closed (``self_check``).

Assumptions (all verified live, see TASKS.md T082 L1-L11):

S1. sudo relays a SIGTERM it receives to the command. It cannot relay SIGKILL.
S2. Without a pty, the command stays in sudo's session, so sid == launcher pid. If sudo instead
    execs directly, the pid is the command's and the sid is the same. ``kill_tree`` therefore
    targets the session id with ``pkill -s <pid>`` run as the OpenCode user.
S3. ``Defaults>brokeoc env_keep = "..."`` applies to commands run as the OpenCode user.
S4. Under env_reset, Ubuntu sudo sets HOME/USER/LOGNAME/SHELL from the target passwd entry and
    PATH from secure_path. sudo keeps the caller's cwd.
S5. With XDG_DATA_HOME unset for the OpenCode user, OpenCode's auth store is
    ``<its home>/.local/share/opencode/auth.json`` (G6).
P1. ``/proc/<pid>/environ`` is mode 0400 and reading it also needs PTRACE_MODE_READ (same uid or
    CAP_SYS_PTRACE), so another unprivileged uid cannot read it. hidepid is not used. The
    self-check probes it anyway.
P2. TIOCSTI: every sudo child is spawned with ``start_new_session=True`` (no controlling tty),
    ``stdin=DEVNULL`` and ``close_fds=True`` (default), so no tty fd is reachable. The sudoers
    file therefore sets ``!use_pty``, which keeps stdin/stdout as our pipes (B9).
W1. WSL interop binfmt entries are named ``WSLInterop`` and/or ``WSLInterop-late`` under
    ``/proc/sys/fs/binfmt_misc/``. Their first line is ``enabled`` or ``disabled``. While one is
    enabled any Linux user can run Windows programs as the Windows account, so the self-check
    fails closed.

Killing: the daemon cannot signal the OpenCode user's processes, so it uses
``sudo -n -u <name> -- /usr/bin/pkill``. Per launch it targets the session id; on every serve
reap/stop and at startup it sweeps with ``pkill -KILL -U <name>`` (the user runs nothing else;
closes N1).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import re
import signal
import subprocess
import sys
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any, Protocol

from app.opencode.auth_store import (
    auth_store_problem as _direct_auth_store_problem,
)
from app.opencode.auth_store import credential_problem, credential_providers_from_text
from app.opencode.env import build_opencode_env
from app.opencode.process import kill_process_tree, resolve_executable, wait_quiet
from app.runtime.interfaces import StartupError

if TYPE_CHECKING:
    from app.config.models import AppConfig

logger = logging.getLogger(__name__)

SUDO = "/usr/bin/sudo"
PKILL = "/usr/bin/pkill"
TEST = "/usr/bin/test"
HEAD = "/usr/bin/head"
SUDO_CHILD_PATH = "/usr/bin:/bin"
KEEP_ENV: tuple[str, ...] = (
    "LANG",
    "OPENCODE_CONFIG_CONTENT",
    "OPENCODE_SERVER_PASSWORD",
    "OPENCODE_SERVER_USERNAME",
)
SUDOERS_NAME_RE = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")
SUDOERS_PATH_RE = re.compile(r"^/[A-Za-z0-9._+/-]+$")
PRIVILEGED_GROUPS = frozenset(
    {"root", "sudo", "admin", "wheel", "adm", "docker", "lxd", "disk", "shadow"}
)
_PROBE_TIMEOUT_S = 10.0
_VERSION_TIMEOUT_S = 30.0
_AUTH_MAX_BYTES = 1_048_576
_BINFMT_DIR = "/proc/sys/fs/binfmt_misc"
ISOLATION_FAILED = "OpenCode user isolation (opencode.os_user) check failed: "
_DIRECT_WARNING = (
    "opencode.os_user.mode is 'none': OpenCode runs as the daemon user and can read its "
    "secrets (residual risk R1); unsupported for production"
)


@dataclass(frozen=True, slots=True)
class UserInfo:
    name: str
    uid: int
    gid: int
    home: str


@dataclass(frozen=True, slots=True)
class ProbeResult:
    returncode: int | None  # None = timeout or OSError
    stdout: bytes = b""
    stderr: bytes = b""


class HostProbe(Protocol):
    def platform(self) -> str: ...

    def euid(self) -> int: ...

    def pid(self) -> int: ...

    def user_by_uid(self, uid: int) -> UserInfo | None: ...

    def user_by_name(self, name: str) -> UserInfo | None: ...

    def group_names(self, user: UserInfo) -> frozenset[str]: ...

    def interop_enabled(self) -> bool: ...

    def is_dir(self, path: str) -> bool: ...

    def killpg(self, pgid: int, sig: str) -> None: ...

    async def run(self, argv: Sequence[str], *, timeout_s: float) -> ProbeResult: ...


class SystemHostProbe:
    """The real host. ``pwd``/``grp`` are imported lazily so this module imports on Windows."""

    def platform(self) -> str:
        return sys.platform

    def euid(self) -> int:
        return os.geteuid()

    def pid(self) -> int:
        return os.getpid()

    @staticmethod
    def _info(entry: Any) -> UserInfo:
        return UserInfo(
            name=str(entry.pw_name),
            uid=int(entry.pw_uid),
            gid=int(entry.pw_gid),
            home=str(entry.pw_dir),
        )

    def user_by_uid(self, uid: int) -> UserInfo | None:
        import pwd

        try:
            return self._info(pwd.getpwuid(uid))
        except (KeyError, OSError):
            return None

    def user_by_name(self, name: str) -> UserInfo | None:
        import pwd

        try:
            return self._info(pwd.getpwnam(name))
        except (KeyError, OSError):
            return None

    def group_names(self, user: UserInfo) -> frozenset[str]:
        import grp

        names: set[str] = set()
        for gid in os.getgrouplist(user.name, user.gid):
            try:
                names.add(grp.getgrgid(gid).gr_name)
            except (KeyError, OSError):
                continue
        return frozenset(names)

    def interop_enabled(self) -> bool:
        try:
            entries = [p for p in Path(_BINFMT_DIR).glob("WSLInterop*")]
        except OSError:
            return True
        for entry in entries:
            try:
                with entry.open("r", encoding="utf-8", errors="replace") as f:
                    first = f.readline().strip()
            except OSError:
                return True
            if first != "disabled":
                return True
        return False

    def is_dir(self, path: str) -> bool:
        return os.path.isdir(path)

    def killpg(self, pgid: int, sig: str) -> None:
        signum = getattr(signal, "SIG" + sig, None)
        if signum is None or pgid <= 0:
            return
        with contextlib.suppress(ProcessLookupError, PermissionError, OSError, AttributeError):
            os.killpg(pgid, signum)

    async def run(self, argv: Sequence[str], *, timeout_s: float) -> ProbeResult:
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env={"PATH": SUDO_CHILD_PATH},
                start_new_session=True,
            )
        except OSError:
            return ProbeResult(None)
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
        except TimeoutError:
            with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
                proc.kill()
            await wait_quiet(proc, 2.0)
            return ProbeResult(None)
        except OSError:
            return ProbeResult(None)
        return ProbeResult(proc.returncode, stdout, stderr)


class OpenCodeLauncher(Protocol):
    """How OpenCode processes are started and killed. Also a RuntimeService."""

    isolated: bool

    def executable(self, command0: str) -> str: ...

    def command(self, argv: Sequence[str]) -> list[str]: ...

    def environment(
        self, environ: Mapping[str, str], *, passthrough: Sequence[str], extra: Mapping[str, str]
    ) -> dict[str, str]: ...

    async def kill_tree(self, proc: asyncio.subprocess.Process, *, grace_s: float) -> None: ...

    async def sweep(self) -> None: ...

    async def auth_store_problem(self) -> str | None: ...

    async def start(self) -> None: ...

    async def stop(self) -> None: ...


class DirectLauncher:
    """Mode ``none``: OpenCode runs as the daemon user (today's behaviour)."""

    isolated = False

    def __init__(self, environ: Mapping[str, str] | None = None) -> None:
        self._environ = environ

    def executable(self, command0: str) -> str:
        return resolve_executable(command0)

    def command(self, argv: Sequence[str]) -> list[str]:
        return list(argv)

    def environment(
        self, environ: Mapping[str, str], *, passthrough: Sequence[str], extra: Mapping[str, str]
    ) -> dict[str, str]:
        return build_opencode_env(environ, passthrough=passthrough, extra=extra)

    async def kill_tree(self, proc: asyncio.subprocess.Process, *, grace_s: float) -> None:
        await kill_process_tree(proc, grace_s=grace_s)

    async def sweep(self) -> None:
        return None

    async def auth_store_problem(self) -> str | None:
        return await asyncio.to_thread(_direct_auth_store_problem, self._environ)

    async def start(self) -> None:
        logger.warning(_DIRECT_WARNING)

    async def stop(self) -> None:
        return None


def _under(path: str, home: str) -> bool:
    """POSIX string comparison: ``path`` is ``home`` or below it."""
    return path == home or path.startswith(home.rstrip("/") + "/")


def _stderr_tail(data: bytes) -> str:
    text = data.decode("utf-8", errors="replace")[-200:]
    text = "".join(c for c in text if unicodedata.category(c)[0] != "C" or c in " ")
    return text.strip()


class SudoLauncher:
    """Mode ``sudo``: every OpenCode process runs as ``opencode.os_user.name`` via sudo."""

    isolated = True

    def __init__(self, config: AppConfig, *, probe: HostProbe) -> None:
        self._config = config
        self._probe = probe
        self.name = config.opencode.os_user.name

    # ---- launching -------------------------------------------------

    def executable(self, command0: str) -> str:
        if PurePosixPath(command0).is_absolute():
            return command0
        raise StartupError(
            "opencode.command[0] must be an absolute path when opencode.os_user.mode is sudo"
        )

    def command(self, argv: Sequence[str]) -> list[str]:
        return [SUDO, "-n", "-u", self.name, "--", *argv]

    def environment(
        self, environ: Mapping[str, str], *, passthrough: Sequence[str], extra: Mapping[str, str]
    ) -> dict[str, str]:
        """Only what sudo's env_keep would keep anyway; ``passthrough`` is ignored (rejected)."""
        env = {"PATH": SUDO_CHILD_PATH}
        if "LANG" in environ:
            env["LANG"] = environ["LANG"]
        env.update({k: v for k, v in extra.items() if k in KEEP_ENV})
        return env

    # ---- killing ---------------------------------------------------

    async def _pkill(self, *args: str) -> ProbeResult | None:
        try:
            result = await self._probe.run(self.command([PKILL, *args]), timeout_s=_PROBE_TIMEOUT_S)
        except Exception as e:
            logger.warning("pkill as OpenCode user failed: %s", type(e).__name__)
            return None
        if result.returncode not in (0, 1):
            logger.warning("pkill as OpenCode user returned %s", result.returncode)
        return result

    async def kill_tree(self, proc: asyncio.subprocess.Process, *, grace_s: float) -> None:
        try:
            pid = proc.pid
            if proc.returncode is not None:
                await self._pkill("-KILL", "-s", str(pid))
                return
            self._probe.killpg(pid, "TERM")
            await self._pkill("-TERM", "-s", str(pid))
            await wait_quiet(proc, grace_s)
            await self._pkill("-KILL", "-s", str(pid))
            self._probe.killpg(pid, "KILL")
            if proc.returncode is None:
                with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
                    proc.kill()
                await wait_quiet(proc, grace_s)
        except Exception as e:
            logger.warning("kill_tree failed: %s", type(e).__name__)

    async def sweep(self) -> None:
        result = await self._pkill("-KILL", "-U", self.name)
        if result is not None and result.returncode == 0:
            logger.info("killed leftover processes of OpenCode user %s", self.name)

    # ---- credential store ------------------------------------------

    async def auth_store_problem(self) -> str | None:
        path = "<unknown>"
        try:
            user = self._probe.user_by_name(self.name)
            if user is None:
                return f"OpenCode user missing: {self.name}"
            path = f"{user.home}/.local/share/opencode/auth.json"
            exists = await self._probe.run(
                self.command([TEST, "-e", path]), timeout_s=_PROBE_TIMEOUT_S
            )
            if exists.returncode == 1:
                return None
            if exists.returncode != 0:
                return credential_problem(None, path)
            head = await self._probe.run(
                self.command([HEAD, "-c", str(_AUTH_MAX_BYTES + 1), "--", path]),
                timeout_s=_PROBE_TIMEOUT_S,
            )
            if head.returncode != 0 or len(head.stdout) > _AUTH_MAX_BYTES:
                return credential_problem(None, path)
            try:
                text = head.stdout.decode("utf-8-sig")
            except UnicodeDecodeError:
                return credential_problem(None, path)
            return credential_problem(credential_providers_from_text(text), path)
        except Exception as e:
            logger.warning("auth store check failed: %s", type(e).__name__)
            return credential_problem(None, path)

    # ---- startup self-check ----------------------------------------

    async def self_check(self) -> None:
        """Raise StartupError at the first failing isolation check. Never sweeps."""
        probe = self._probe
        oc_cfg = self._config.opencode
        name = self.name

        def fail(reason: str) -> StartupError:
            return StartupError(ISOLATION_FAILED + reason)

        if not probe.platform().startswith("linux"):
            raise fail(
                "mode 'sudo' needs Linux (WSL); set opencode.os_user.mode: none for tests/dev "
                "(unsupported for production)"
            )
        euid = probe.euid()
        if euid == 0:
            raise fail("the daemon must not run as root")
        daemon = probe.user_by_uid(euid)
        if daemon is None:
            raise fail("cannot look up the daemon user")
        oc = probe.user_by_name(name)
        if oc is None:
            raise fail(
                f"user '{name}' does not exist; create it (docs/SETUP-discord-opencode.md, Part C6)"
            )
        if oc.uid in (0, daemon.uid):
            raise fail(f"user '{name}' must differ from the daemon user and must not be root")
        bad = probe.group_names(oc) & PRIVILEGED_GROUPS
        if bad:
            raise fail(
                f"user '{name}' is in privileged group(s) {', '.join(sorted(bad))}; "
                f"remove it (sudo gpasswd -d {name} <group>)"
            )
        exe = oc_cfg.command[0]
        if not PurePosixPath(exe).is_absolute():
            raise fail(
                "opencode.command[0] must be an absolute path (e.g. /usr/local/bin/opencode)"
            )
        if _under(exe, daemon.home):
            raise fail("opencode.command[0] must not be inside the daemon user's home")
        if oc_cfg.env_passthrough:
            raise fail("opencode.env_passthrough must be empty (sudo resets the environment)")
        server_dir = oc_cfg.server_directory
        if oc_cfg.managed_server and (
            not PurePosixPath(server_dir).is_absolute() or _under(server_dir, daemon.home)
        ):
            raise fail(
                "opencode.server_directory must be an absolute path outside the daemon user's "
                "home (e.g. /var/lib/broke-ai-coder/opencode-server)"
            )
        if probe.interop_enabled():
            raise fail(
                "WSL interop is enabled: the OpenCode user could start Windows programs as your "
                "Windows account; set [interop] enabled=false in /etc/wsl.conf, then run "
                "wsl --shutdown"
            )
        version = await probe.run(
            self.command([exe, *oc_cfg.command[1:], "--version"]), timeout_s=_VERSION_TIMEOUT_S
        )
        if version.returncode != 0:
            if version.returncode is None:
                detail = "timed out"
            else:
                detail = f"exit {version.returncode}"
                tail = _stderr_tail(version.stderr)
                if tail:
                    detail += ": " + tail
            raise fail(
                f"`sudo -n -u {name} -- {exe} --version` failed ({detail}); install the "
                "sudoers file from `python -m app --print-sudoers`"
            )

        home = daemon.home.rstrip("/")
        targets: list[tuple[str, str]] = [(daemon.home, "-r"), (daemon.home, "-x")]
        for entry in self._config.opencode.os_user.protected_paths:
            path = home + "/" + entry[2:] if entry.startswith("~/") else entry
            targets.append((path, "-r"))
        targets.append((os.path.abspath(self._config.database.path), "-r"))
        targets.append((f"/proc/{probe.pid()}/environ", "-r"))
        for path, flag in targets:
            r = await probe.run(self.command([TEST, flag, path]), timeout_s=_PROBE_TIMEOUT_S)
            if r.returncode == 1:
                continue
            if r.returncode == 0:
                raise fail(
                    f"user '{name}' can access {path} ({flag}); fix permissions (Part C6: "
                    "chmod 700 your home, chmod 600 the secrets file, automount umask=077 in "
                    "/etc/wsl.conf)"
                )
            raise fail(f"access probe failed for {path}")

        if oc_cfg.managed_server:
            for flag in ("-w", "-x"):
                r = await probe.run(
                    self.command([TEST, flag, server_dir]), timeout_s=_PROBE_TIMEOUT_S
                )
                if r.returncode != 0:
                    raise fail(f"user '{name}' cannot write opencode.server_directory {server_dir}")

        for directory in [oc_cfg.working_directory, *oc_cfg.projects.values()]:
            if not probe.is_dir(directory):
                continue
            for flag in ("-w", "-x"):
                r = await probe.run(
                    self.command([TEST, flag, directory]), timeout_s=_PROBE_TIMEOUT_S
                )
                if r.returncode != 0:
                    logger.warning(
                        "OpenCode user %s cannot write project directory %s; "
                        "tasks there cannot edit files",
                        name,
                        directory,
                    )
                    break

    # ---- RuntimeService --------------------------------------------

    async def start(self) -> None:
        await self.self_check()
        await self.sweep()
        user = self._probe.user_by_name(self.name)
        logger.info("OpenCode runs as %s (uid %d) via sudo", self.name, user.uid if user else -1)

    async def stop(self) -> None:
        await self.sweep()


def render_sudoers(daemon_user: str, oc_user: str, opencode_path: str) -> str:
    """The sudoers drop-in letting ``daemon_user`` run four commands as ``oc_user`` only."""
    for label, value in (("daemon user", daemon_user), ("OpenCode user", oc_user)):
        if SUDOERS_NAME_RE.fullmatch(value) is None:
            raise ValueError(f"{label} name {value!r} is not a valid user name")
        if value in ("root", "ALL"):
            raise ValueError(f"{label} must not be {value}")
    if daemon_user == oc_user:
        raise ValueError("the daemon user and the OpenCode user must differ")
    if SUDOERS_PATH_RE.fullmatch(opencode_path) is None:
        raise ValueError(
            f"opencode.command[0] {opencode_path!r} must be an absolute path of plain characters"
        )
    keep = " ".join(KEEP_ENV)
    return (
        "# /etc/sudoers.d/broke-ai-coder (generated by `python -m app --print-sudoers`)\n"
        f"# Lets {daemon_user} run OpenCode and three helper tools as {oc_user} only - never as "
        "root - without a\n"
        f"# password. {oc_user} is the less privileged user, so this grants {daemon_user} "
        "nothing it lacks.\n"
        "# Validate with `visudo -cf` before installing (docs/SETUP-discord-opencode.md, "
        "Part C6).\n"
        f"Defaults>{oc_user} env_reset\n"
        f'Defaults>{oc_user} env_keep = "{keep}"\n'
        f"Defaults>{oc_user} !use_pty\n"
        f"Defaults>{oc_user} !requiretty\n"
        f"{daemon_user} ALL=({oc_user}) NOPASSWD: {opencode_path}, {PKILL}, {TEST}, {HEAD}\n"
    )


def build_launcher(
    config: AppConfig,
    *,
    environ: Mapping[str, str] | None = None,
    probe: HostProbe | None = None,
) -> OpenCodeLauncher:
    if config.opencode.os_user.mode == "none":
        return DirectLauncher(environ)
    return SudoLauncher(config, probe=probe or SystemHostProbe())
