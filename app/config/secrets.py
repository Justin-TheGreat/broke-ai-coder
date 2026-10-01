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


def collect_secret_values(
    config: AppConfig, environ: Mapping[str, str] | None = None
) -> frozenset[str]:
    names = [prov.api_key_env for prov in config.providers.values()]
    names.append(config.discord.bot_token_env)
    values = (resolve_secret(n, environ) for n in names)
    return frozenset(v for v in values if v is not None)
