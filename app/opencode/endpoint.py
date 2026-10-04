from __future__ import annotations

from dataclasses import dataclass

import httpx


@dataclass(frozen=True, slots=True)
class ServerEndpoint:
    """Where the OpenCode server listens, plus its Basic-auth credentials when protected."""

    url: str
    password: str | None = None
    username: str = "opencode"

    def auth(self) -> httpx.BasicAuth | None:
        if self.password is None:
            return None
        return httpx.BasicAuth(self.username, self.password)

    def __repr__(self) -> str:
        return f"ServerEndpoint(url={self.url!r}, password={'<set>' if self.password else None})"
