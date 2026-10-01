from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from app.config.models import AppConfig


class ConfigError(Exception):
    pass


def parse_config(data: Mapping[str, Any] | None) -> AppConfig:
    try:
        return AppConfig.model_validate(dict(data or {}))
    except ValidationError as e:
        raise ConfigError(f"invalid config: {e}") from e


def load_config(path: str | os.PathLike[str]) -> AppConfig:
    p = Path(path)
    if not p.is_file():
        raise ConfigError(f"config file not found: {path}")
    try:
        data = yaml.safe_load(p.read_text(encoding="utf-8"))
    except yaml.YAMLError as e:
        raise ConfigError(f"invalid YAML in {path}: {e}") from e
    if data is None:
        data = {}
    if not isinstance(data, Mapping):
        raise ConfigError(f"config top level must be a mapping: {path}")
    return parse_config(data)
