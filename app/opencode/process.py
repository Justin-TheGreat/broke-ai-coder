from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal
import subprocess
import sys

logger = logging.getLogger(__name__)

_TASKKILL_TIMEOUT_S = 10.0


def _killpg(pid: int, sig: int) -> None:
    with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
        os.killpg(pid, sig)


async def _wait(proc: asyncio.subprocess.Process, timeout: float) -> None:
    with contextlib.suppress(TimeoutError, Exception):
        await asyncio.wait_for(proc.wait(), timeout=timeout)


async def kill_process_tree(proc: asyncio.subprocess.Process, *, grace_s: float) -> None:
    """Kill ``proc`` and its descendants. Never raises.

    POSIX: the process was started with ``start_new_session=True`` so its pid is the
    process group id. Windows: the process was started with CREATE_NEW_PROCESS_GROUP.
    Accepted limitation: on Windows, grandchildren of an already-exited root cannot be
    found through ``taskkill /T``.
    """
    try:
        if sys.platform == "win32":
            await _kill_windows(proc, grace_s)
        else:
            await _kill_posix(proc, grace_s)
    except Exception as e:
        logger.warning("kill_process_tree failed: %s", type(e).__name__)


async def _kill_posix(proc: asyncio.subprocess.Process, grace_s: float) -> None:
    if proc.returncode is not None:
        _killpg(proc.pid, signal.SIGKILL)  # reap orphans of an already-exited root
        return
    _killpg(proc.pid, signal.SIGTERM)
    await _wait(proc, grace_s)
    _killpg(proc.pid, signal.SIGKILL)
    if proc.returncode is None:
        with contextlib.suppress(ProcessLookupError, OSError):
            proc.kill()
        await _wait(proc, grace_s)


async def _kill_windows(proc: asyncio.subprocess.Process, grace_s: float) -> None:
    if proc.returncode is not None:
        return
    try:
        killer = await asyncio.create_subprocess_exec(
            "taskkill",
            "/PID",
            str(proc.pid),
            "/T",
            "/F",
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        try:
            await asyncio.wait_for(killer.wait(), timeout=_TASKKILL_TIMEOUT_S)
        except TimeoutError:
            with contextlib.suppress(ProcessLookupError, OSError):
                killer.kill()
    except OSError as e:
        logger.warning("taskkill failed to run: %s", type(e).__name__)
    if proc.returncode is None:
        with contextlib.suppress(ProcessLookupError, OSError):
            proc.kill()
    await _wait(proc, grace_s)
