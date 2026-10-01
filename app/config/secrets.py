from __future__ import annotations

import os
from collections.abc import Mapping

from app.config.models import AppConfig


def resolve_secret(env_name: str, environ: Mapping[str, str] | None = None) -> str | None:
    env = os.environ if environ is None else environ
    value = env.get(env_name)
    if value is None or not value.strip():
        return None
    return value


def credentials_present(
    config: AppConfig, environ: Mapping[str, str] | None = None
) -> frozenset[str]:
    return frozenset(
        name
        for name, prov in config.providers.items()
        if resolve_secret(prov.api_key_env, environ) is not None
    )
