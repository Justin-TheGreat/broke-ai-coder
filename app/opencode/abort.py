"""Best-effort abort of a server-side OpenCode session.

B11. ``POST {server_url}/session/{id}/abort`` asks the OpenCode server to stop the agent loop
     of that session. With ``opencode run --attach`` the loop and its tools run inside the
     server, so killing the CLI client alone does not stop them. The call is best effort:
     it never raises and logs only the exception class name.
"""

from __future__ import annotations

import logging

import httpx

from app.opencode.events import SESSION_ID_RE

logger = logging.getLogger(__name__)

ABORT_PATH_TEMPLATE = "/session/{session_id}/abort"


async def abort_opencode_session(
    server_url: str,
    session_id: str | None,
    *,
    timeout_s: float,
    transport: httpx.AsyncBaseTransport | None = None,
    auth: httpx.Auth | None = None,
) -> bool:
    """Return True when the server answered 2xx. Never raises."""
    if session_id is None or SESSION_ID_RE.fullmatch(session_id) is None:
        return False
    url = server_url.rstrip("/") + ABORT_PATH_TEMPLATE.format(session_id=session_id)
    try:
        async with httpx.AsyncClient(
            transport=transport,
            timeout=timeout_s,
            follow_redirects=False,
            trust_env=False,
            auth=auth,
        ) as client:
            response = await client.post(url)
        return response.is_success
    except Exception as e:
        logger.warning("opencode session abort failed: %s", type(e).__name__)
        return False
