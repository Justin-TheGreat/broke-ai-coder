"""Child-process environment allowlist for OpenCode.

OpenCode must never see a provider key, the Discord bot token, or an out-of-band config
variable. An allowlist (instead of a blocklist) makes that true by construction: any variable
that is not named here or explicitly passed through is absent.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

BASE_ENV_NAMES = frozenset(
    {
        "PATH",
        "HOME",
        "USER",
        "LOGNAME",
        "SHELL",
        "LANG",
        "LANGUAGE",
        "TERM",
        "TZ",
        "TMPDIR",
        "TEMP",
        "TMP",
        "XDG_CONFIG_HOME",
        "XDG_DATA_HOME",
        "XDG_CACHE_HOME",
        "XDG_STATE_HOME",
        "XDG_RUNTIME_DIR",
        "SYSTEMROOT",
        "WINDIR",
        "COMSPEC",
        "PATHEXT",
        "USERPROFILE",
        "APPDATA",
        "LOCALAPPDATA",
        "PROGRAMDATA",
    }
)


def build_opencode_env(
    environ: Mapping[str, str], *, passthrough: Sequence[str], extra: Mapping[str, str]
) -> dict[str, str]:
    """Allowlisted base names, ``LC_*``, present passthrough names, then ``extra`` on top."""
    wanted = BASE_ENV_NAMES | set(passthrough)
    env = {k: v for k, v in environ.items() if k in wanted or k.startswith("LC_")}
    env.update(extra)
    return env
