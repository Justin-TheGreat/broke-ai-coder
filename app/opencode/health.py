"""OpenCode server health check.

B7. ``GET {server_url}/global/health`` returns ``{"healthy": true, "version": "<str>"}``.
    The server runs without auth on localhost, so Basic auth (OPENCODE_SERVER_PASSWORD) is
    not supported in this slice.
"""

from __future__ import annotations

import httpx

from app.runtime.interfaces import ExecutorHealth

_VERSION_CAP = 64


async def check_opencode_health(
    server_url: str,
    *,
    timeout_s: float,
    transport: httpx.AsyncBaseTransport | None = None,
) -> ExecutorHealth:
    url = server_url.rstrip("/") + "/global/health"
    try:
        async with httpx.AsyncClient(
            transport=transport, timeout=timeout_s, follow_redirects=False
        ) as client:
            response = await client.get(url)
        if response.status_code != 200:
            return ExecutorHealth(False, None, f"HTTP {response.status_code}")
        try:
            body = response.json()
        except ValueError:
            return ExecutorHealth(False, None, "malformed response")
        if not isinstance(body, dict):
            return ExecutorHealth(False, None, "malformed response")
        if body.get("healthy") is not True:
            return ExecutorHealth(False, None, "unhealthy")
        version = body.get("version")
        return ExecutorHealth(
            True, version[:_VERSION_CAP] if isinstance(version, str) else None, "ok"
        )
    except Exception as e:
        return ExecutorHealth(False, None, type(e).__name__)
