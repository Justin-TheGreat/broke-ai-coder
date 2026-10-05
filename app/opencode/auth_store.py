"""OpenCode's stored-credential file (``opencode auth login``).

G6. Credentials live at ``$XDG_DATA_HOME/opencode/auth.json`` (default
    ``~/.local/share/opencode/auth.json``), a JSON object keyed by provider id. OpenCode must
    hold no provider keys, so any entry is a problem. Only the keys are read; values are never
    returned or logged.

S5. With XDG_DATA_HOME unset for the OpenCode user (sudo env_reset), the store is
    ``<that user's home>/.local/share/opencode/auth.json``; see ``app/opencode/os_user.py``.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping
from pathlib import Path

_MAX_KEYS = 10
_MAX_KEY_CHARS = 40
_BAD_CHARS = re.compile(r"[^A-Za-z0-9._-]")


def auth_store_path(environ: Mapping[str, str] | None = None) -> Path:
    env = os.environ if environ is None else environ
    data_home = env.get("XDG_DATA_HOME")
    base = Path(data_home) if data_home else Path.home() / ".local" / "share"
    return base / "opencode" / "auth.json"


def credential_providers_from_text(text: str) -> list[str] | None:
    """Sorted, sanitized top-level keys of a JSON object; None when it is not a JSON object."""
    try:
        data = json.loads(text)
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    keys = sorted(_BAD_CHARS.sub("?", str(k))[:_MAX_KEY_CHARS] for k in data)
    if len(keys) > _MAX_KEYS:
        return [*keys[:_MAX_KEYS], f"+{len(keys) - _MAX_KEYS} more"]
    return keys


def credential_problem(keys: list[str] | None, path: str | Path) -> str | None:
    """The problem message for ``keys`` (None means unreadable), or None when there is none."""
    if keys is None:
        return f"OpenCode credential store unreadable: {path}"
    if keys:
        return (
            f"OpenCode has stored provider credentials for: {', '.join(keys)}. "
            f"Delete the credential store as the OpenCode user (`rm -f {path}`; setup guide E3) "
            "rather than running `opencode auth logout`, which loads agent-writable config. "
            "OpenCode must hold no provider keys."
        )
    return None


def stored_credential_providers(environ: Mapping[str, str] | None = None) -> list[str] | None:
    """Sorted, sanitized provider ids with stored credentials; None when unreadable/unsafe."""
    path = auth_store_path(environ)
    try:
        text = path.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        return []
    except OSError:
        return None
    return credential_providers_from_text(text)


def auth_store_problem(environ: Mapping[str, str] | None = None) -> str | None:
    return credential_problem(stored_credential_providers(environ), auth_store_path(environ))
