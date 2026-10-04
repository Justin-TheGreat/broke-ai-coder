"""Controller-managed ``opencode serve`` pinned to the key-holding gateway.

The daemon launches ``opencode serve`` itself with:

* a key-free environment (allowlist, ``app/opencode/env.py``),
* ``OPENCODE_CONFIG_CONTENT`` carrying the whole safety config: ``enabled_providers`` limited to
  the gateway provider ``broke`` whose ``baseURL`` is the loopback gateway and whose ``apiKey`` is
  the random per-run gateway token, plus the pinned ``model`` and ``small_model``,
* a random Basic-auth password (``OPENCODE_SERVER_PASSWORD``) bound to 127.0.0.1,
* a daemon-owned working directory (``opencode.server_directory``).

Assumptions (verify at T082): G4 (``opencode run --attach`` reads the password from its own
environment), G5 (a custom-provider model's ``limit`` is optional), G7 (``OPENCODE_CONFIG_CONTENT``
outranks global/project files; ``GET /config`` returns the merged config), G8 (``{env:``/``{file:``
substitution also applies to the inline config, so model ids with braces are rejected at load).

OS user (T072, closes R1): with ``opencode.os_user.mode: sudo`` (default) serve is started through
``sudo -n -u <os_user.name> --`` (``app/opencode/os_user.py``, which lists the assumptions S1-S5),
so it runs as a dedicated unprivileged user with its own HOME. It cannot read the daemon's home,
secrets file or ``/proc/<pid>/environ``. Mode ``none`` (tests/dev) runs it as the daemon user and
keeps the old exposure. Every serve reap sweeps all of that user's processes (N1).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import secrets
import socket
import subprocess
import time
from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING, Any

import httpx

from app.config.models import GATEWAY_PROVIDER_ID, AppConfig
from app.opencode.endpoint import ServerEndpoint
from app.opencode.health import check_opencode_health
from app.opencode.os_user import DirectLauncher, OpenCodeLauncher
from app.opencode.process import spawn_kwargs
from app.redaction import SecretRedactor
from app.runtime.interfaces import StartupError

if TYPE_CHECKING:
    from app.gateway.server import LlmGateway

logger = logging.getLogger(__name__)

_HEALTH_POLL_S = 0.25
_INITIAL_BACKOFF_S = 1.0
_BACKOFF_RESET_S = 60.0  # uptime after which the restart backoff starts over
_DETAIL_CAP = 300


def build_opencode_config(
    config: AppConfig, *, gateway_base_url: str, gateway_token: str
) -> dict[str, Any]:
    """The inline OpenCode config: gateway as the only provider, models pinned."""
    models = config.gateway_models()
    if not models:
        raise StartupError("no enabled allowlisted models; check routing.provider_order/policies")
    entries: dict[str, Any] = {}
    for gid, (provider, model) in models.items():
        entry: dict[str, Any] = {"name": gid}
        meta = config.providers[provider].models.get(model)
        if meta is not None and meta.context_window and meta.max_output_tokens:
            entry["limit"] = {"context": meta.context_window, "output": meta.max_output_tokens}
        entries[gid] = entry
    first = next(iter(models))
    small = config.gateway.small_model or first
    return {
        "$schema": "https://opencode.ai/config.json",
        "enabled_providers": [GATEWAY_PROVIDER_ID],
        "provider": {
            GATEWAY_PROVIDER_ID: {
                "npm": "@ai-sdk/openai-compatible",
                "name": "broke-ai-coder gateway",
                "options": {"baseURL": gateway_base_url, "apiKey": gateway_token},
                "models": entries,
            }
        },
        "model": f"{GATEWAY_PROVIDER_ID}/{first}",
        "small_model": f"{GATEWAY_PROVIDER_ID}/{small}",
        "share": "disabled",
        "autoupdate": False,
    }


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _port_in_use(port: int) -> bool:
    """True if something already listens on (or holds) 127.0.0.1:port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        if os.name == "posix":
            # Tolerate our own TIME_WAIT leftovers; a live listener still fails the bind.
            # Never set SO_REUSEADDR on Windows: there it allows binding over a live listener.
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(("127.0.0.1", port))
        except OSError:
            return True
    return False


class _ServeFailed(Exception):
    """Internal: one launch attempt failed; the message is a redacted short detail."""


class _PortInUse(_ServeFailed):
    """Internal: the serve port is held by something else; retrying cannot help."""


class OpenCodeServerManager:
    def __init__(
        self,
        app_config: AppConfig,
        *,
        gateway: LlmGateway,
        redactor: SecretRedactor | None = None,
        environ: Mapping[str, str] | None = None,
        password: str | None = None,
        health_transport: httpx.AsyncBaseTransport | None = None,
        launcher: OpenCodeLauncher | None = None,
    ) -> None:
        self._app = app_config
        self._launcher: OpenCodeLauncher = launcher or DirectLauncher(environ)
        self._oc = app_config.opencode
        self._gateway = gateway
        self._redactor = redactor
        self._environ = environ
        self._health_transport = health_transport
        self.password: str = password if password is not None else secrets.token_urlsafe(24)
        self._port: int | None = None
        self._proc: asyncio.subprocess.Process | None = None
        self._drain: asyncio.Task[None] | None = None
        self._monitor: asyncio.Task[None] | None = None
        self._stderr = bytearray()
        self._executable: str | None = None
        self._stopping = False
        self._launch_listeners: list[Callable[[], None]] = []
        self._fatal_error: str | None = None
        self._ready = False

    @property
    def fatal_error(self) -> str | None:
        return self._fatal_error

    @fatal_error.setter
    def fatal_error(self, value: str | None) -> None:
        self._fatal_error = value
        if value is not None:
            self._ready = False

    @property
    def ready(self) -> bool:
        """True only after a successful health wait; cleared on exit, reap, stop and fatal."""
        return self._ready

    def unavailable_reason(self) -> str | None:
        """None when tasks may attach; else a short, secret-free reason."""
        if self.ready:
            return None
        if self._fatal_error is not None:
            return (
                "managed OpenCode server is down and will not restart by itself; fix the cause,"
                " then restart the daemon: " + self._fatal_error
            )[:_DETAIL_CAP]
        return "managed OpenCode server is down: restarting"

    def add_launch_listener(self, listener: Callable[[], None]) -> None:
        """Called when our serve exits and at every (re)launch attempt (before the spawn)."""
        self._launch_listeners.append(listener)

    def _notify_launch(self) -> None:
        for listener in self._launch_listeners:
            try:
                listener()
            except Exception as e:
                logger.warning("launch listener failed: %s", type(e).__name__)

    @property
    def endpoint(self) -> ServerEndpoint:
        port = self._port if self._port is not None else self._oc.server_port
        return ServerEndpoint(f"http://127.0.0.1:{port}", self.password)

    # ---- lifecycle -------------------------------------------------

    async def start(self) -> None:
        problem = await self._launcher.auth_store_problem()
        if problem is not None:
            raise StartupError(problem)
        self._executable = self._launcher.executable(self._oc.command[0])
        await asyncio.to_thread(lambda: os.makedirs(self._oc.server_directory, exist_ok=True))
        self._stopping = False
        try:
            await self._launch()
        except _ServeFailed as e:
            await self._reap()
            raise StartupError(f"opencode serve did not become healthy: {e}") from None
        except BaseException:
            await self._reap()
            raise
        self._monitor = asyncio.create_task(self._monitor_loop(), name="opencode-serve-monitor")

    async def stop(self) -> None:
        self._stopping = True
        self._ready = False
        monitor, self._monitor = self._monitor, None
        if monitor is not None:
            monitor.cancel()
            await asyncio.gather(monitor, return_exceptions=True)
        await self._reap()

    # ---- internals -------------------------------------------------

    def _stderr_detail(self) -> str:
        text = bytes(self._stderr).decode("utf-8", errors="replace").strip()
        if self._redactor is not None:
            text = self._redactor.redact(text)
        return text

    def _detail(self, head: str) -> str:
        tail = self._stderr_detail()
        return (f"{head}: {tail}" if tail else head)[-_DETAIL_CAP:]

    async def _drain_stderr(self, proc: asyncio.subprocess.Process) -> None:
        keep = self._oc.stderr_tail_bytes
        stream = proc.stderr
        assert stream is not None
        while True:
            chunk = await stream.read(4096)
            if not chunk:
                return
            self._stderr += chunk
            if len(self._stderr) > keep:
                del self._stderr[: len(self._stderr) - keep]

    async def _reap(self) -> None:
        self._ready = False
        proc, self._proc = self._proc, None
        if proc is not None:
            await self._launcher.kill_tree(proc, grace_s=self._oc.kill_grace_s)
        drain, self._drain = self._drain, None
        if drain is not None:
            drain.cancel()
            await asyncio.gather(drain, return_exceptions=True)
        # N1: nothing the OpenCode user started may outlive this serve.
        with contextlib.suppress(Exception):
            await self._launcher.sweep()

    async def _spawn(self) -> asyncio.subprocess.Process:
        assert self._executable is not None
        self._notify_launch()
        port = self._oc.server_port
        if port:
            if await asyncio.to_thread(_port_in_use, port):
                raise _PortInUse(
                    f"port {port} already in use - stop any manually started `opencode serve`"
                )
        else:
            port = await asyncio.to_thread(_free_port)
            if await asyncio.to_thread(_port_in_use, port):
                raise _PortInUse(f"port {port} already in use")
        self._port = port
        cfg = build_opencode_config(
            self._app,
            gateway_base_url=self._gateway.base_url,
            gateway_token=self._gateway.token,
        )
        env = self._launcher.environment(
            os.environ if self._environ is None else self._environ,
            passthrough=self._oc.env_passthrough,
            extra={
                "OPENCODE_CONFIG_CONTENT": json.dumps(cfg),
                "OPENCODE_SERVER_PASSWORD": self.password,
                "OPENCODE_SERVER_USERNAME": "opencode",
            },
        )
        argv = self._launcher.command(
            [
                self._executable,
                *self._oc.command[1:],
                "serve",
                "--hostname",
                "127.0.0.1",
                "--port",
                str(port),
            ]
        )
        self._stderr = bytearray()
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                cwd=self._oc.server_directory,
                env=env,
                **spawn_kwargs(),
            )
        except OSError as e:
            raise _ServeFailed(type(e).__name__) from None
        self._proc = proc
        self._drain = asyncio.create_task(self._drain_stderr(proc), name="opencode-serve-stderr")
        return proc

    async def _wait_healthy(self, proc: asyncio.subprocess.Process) -> None:
        timeout = self._oc.server_start_timeout_s
        deadline = time.monotonic() + timeout
        endpoint = self.endpoint
        while True:
            if proc.returncode is not None:
                # Let the drain task see EOF so the stderr tail is complete.
                if self._drain is not None:
                    await asyncio.wait({self._drain}, timeout=1.0)
                raise _ServeFailed(self._detail(f"exit code {proc.returncode}"))
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise _ServeFailed(self._detail(f"no healthy response in {timeout:g}s"))
            health = await check_opencode_health(
                endpoint.url,
                timeout_s=max(0.1, min(2.0, remaining)),
                transport=self._health_transport,
                auth=endpoint.auth(),
            )
            if health.healthy:
                # Our server enforces the password: an unauthenticated probe must get 401. A
                # foreign server (no auth) is rejected, and our child must still be alive.
                anon = await check_opencode_health(
                    endpoint.url,
                    timeout_s=max(0.1, min(2.0, remaining)),
                    transport=self._health_transport,
                    auth=None,
                )
                if anon.detail == "HTTP 401" and proc.returncode is None:
                    return
                if anon.healthy:
                    hint = (
                        " (or the sudoers env_keep line is missing, so serve got no password)"
                        if self._launcher.isolated
                        else ""
                    )
                    raise _PortInUse(
                        f"port {self._port} is served by a server that does not enforce the "
                        f"password - stop any manually started `opencode serve`{hint}"
                    )
            await asyncio.sleep(min(_HEALTH_POLL_S, max(0.0, remaining)))

    async def _launch(self) -> None:
        proc = await self._spawn()
        await self._wait_healthy(proc)
        if proc.returncode is None:
            self._ready = True

    async def _monitor_loop(self) -> None:
        backoff = _INITIAL_BACKOFF_S
        cap = self._oc.server_restart_max_backoff_s
        while not self._stopping:
            try:
                proc = self._proc
                started = time.monotonic()
                if proc is not None:
                    await proc.wait()
                if self._stopping:
                    return
                # Invalidate at the moment of the crash, not only at relaunch: the backoff
                # window must not honour a cached "server config OK" for an unknown listener.
                self._ready = False
                self._notify_launch()
                code = proc.returncode if proc is not None else None
                logger.warning(
                    "opencode serve exited code=%s stderr=%s", code, self._stderr_detail()[-200:]
                )
                if time.monotonic() - started >= _BACKOFF_RESET_S:
                    backoff = _INITIAL_BACKOFF_S
                await self._reap()
                while not self._stopping:
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, cap)
                    if self._stopping:
                        return
                    try:
                        await self._launch()
                        break
                    except _PortInUse as e:
                        self.fatal_error = str(e)
                        logger.error(
                            "opencode serve cannot restart; giving up until the daemon is"
                            " restarted: %s",
                            e,
                        )
                        await self._reap()
                        return
                    except _ServeFailed as e:
                        logger.warning("opencode serve restart failed: %s", e)
                        await self._reap()
                    except StartupError as e:
                        logger.warning("opencode serve restart failed: %s", e)
                        await self._reap()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning("opencode serve monitor error: %s", type(e).__name__)
                with contextlib.suppress(Exception):
                    await self._reap()
                await asyncio.sleep(min(backoff, cap))
