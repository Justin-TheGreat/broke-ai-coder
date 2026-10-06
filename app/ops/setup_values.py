"""Print the config values the setup scripts need as ``KEY=value`` lines (never any secret).

Run as ``python -m app.ops.setup_values --config PATH``. Every value is restricted to
``[A-Za-z0-9._/+-]`` so shell scripts can parse the output with ``IFS='=' read -r`` and never
need ``eval``.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from collections.abc import Sequence

from app.config.loader import ConfigError, load_config
from app.config.models import AppConfig

_SAFE_RE = re.compile(r"^[A-Za-z0-9._/+-]+$")


def setup_values(config: AppConfig) -> list[tuple[str, str]]:
    oc = config.opencode
    return [
        ("OS_USER_MODE", oc.os_user.mode),
        ("OC_USER", oc.os_user.name),
        ("OPENCODE_BIN", oc.command[0]),
        ("SERVER_DIRECTORY", oc.server_directory),
        ("WORKING_DIRECTORY", oc.working_directory),
        ("DATABASE_PATH", os.path.abspath(config.database.path)),
        ("MANAGED_SERVER", "true" if oc.managed_server else "false"),
    ]


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="app.ops.setup_values")
    parser.add_argument("--config", required=True)
    args = parser.parse_args(argv)
    try:
        config = load_config(args.config)
    except ConfigError as e:
        print(str(e), file=sys.stderr)
        return 2
    values = setup_values(config)
    for key, value in values:
        if not _SAFE_RE.match(value):
            print(f"unsafe value for {key}", file=sys.stderr)
            return 2
    for key, value in values:
        print(f"{key}={value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
