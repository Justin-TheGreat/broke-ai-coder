"""Fail-closed checks of the OpenCode server's config (FREE-ONLY safety).

The authoritative guarantee is structural: OpenCode holds no provider keys (its environment is
an allowlist, ``opencode auth`` storage must be empty) and its only provider is the key-holding
gateway, which forwards allowlisted free models only. The checks below detect accidents and
drift; they are not what keeps paid models unreachable.

B12. OpenCode makes background calls (e.g. title generation) with ``small_model``. The daemon
     pins ``model`` and ``small_model`` to ``broke/<gateway model id>`` through
     ``OPENCODE_CONFIG_CONTENT`` and ``enabled_providers: ["broke"]``. ``GET /config`` returns
     the effective merged config; before each task it must show exactly that shape (checks 1-5
     in ``check_server_config``) and ``opencode auth`` storage must be empty (check 0).

B13. OpenCode merges configs (later wins): global ``~/.config/opencode/opencode.json``, the
     ``OPENCODE_CONFIG`` file, the project's ``opencode.json``, ``.opencode/`` directories, and
     ``OPENCODE_CONFIG_CONTENT`` (which outranks the files; managed/MDM system configs outrank
     it, residual risk R3). ``GET /config`` has no documented per-request directory selector,
     so it reflects the server's own start directory only. Therefore, before each task, the
     project's on-disk config is checked too, as defense in depth: ``opencode.json``/
     ``opencode.jsonc`` and ``*.json``/``*.jsonc`` directly in ``.opencode/``, in the project
     dir and every parent up to the filesystem root. Unparseable files are UNSAFE, and a
     project file must not contain ``provider``, ``enabled_providers`` or
     ``disabled_providers``. OPENCODE_* variables never reach the OpenCode child (env
     allowlist).
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import httpx

from app.config.models import GATEWAY_PROVIDER_ID, AppConfig
from app.opencode.auth_store import auth_store_problem
from app.opencode.endpoint import ServerEndpoint
from app.runtime.interfaces import SmallModelCheck

_NPM_PACKAGE = "@ai-sdk/openai-compatible"
_PROJECT_FORBIDDEN_KEYS = ("provider", "enabled_providers", "disabled_providers")
_MODEL_SECTIONS = ("agent", "mode", "command")


def opencode_model_ids(config: AppConfig) -> frozenset[str]:
    """Allowlisted models in OpenCode form: ``broke/<provider>/<model>``."""
    return frozenset(f"{GATEWAY_PROVIDER_ID}/{gid}" for gid in config.gateway_models())


def _bad_model_key(cfg: dict[str, Any], allowed: frozenset[str]) -> str | None:
    """Key path of the first non-allowlisted model value, or None.

    Checked: ``model``, ``small_model``, ``agent.<n>.model``, ``mode.<n>.model``,
    ``command.<n>.model``. A value that is present but not an allowlisted string is bad.
    """
    for key in ("model", "small_model"):
        if key in cfg and not (isinstance(cfg[key], str) and cfg[key] in allowed):
            return key
    for section in _MODEL_SECTIONS:
        if section not in cfg:
            continue
        entries = cfg[section]
        if not isinstance(entries, dict):
            return section
        for name, entry in entries.items():
            if not isinstance(entry, dict):
                return f"{section}.{name}"
            if "model" in entry and not (
                isinstance(entry["model"], str) and entry["model"] in allowed
            ):
                return f"{section}.{name}.model"
    return None


def _gateway_shape_problem(
    body: dict[str, Any], allowed: frozenset[str], gateway_base_url: str | None
) -> str | None:
    """Key path of the first deviation from the gateway-only shape, or None."""
    if body.get("enabled_providers") != [GATEWAY_PROVIDER_ID]:
        return "enabled_providers"
    providers = body.get("provider")
    if not isinstance(providers, dict) or set(providers) != {GATEWAY_PROVIDER_ID}:
        return "provider"
    broke = providers[GATEWAY_PROVIDER_ID]
    if not isinstance(broke, dict):
        return f"provider.{GATEWAY_PROVIDER_ID}"
    if broke.get("npm") != _NPM_PACKAGE:
        return f"provider.{GATEWAY_PROVIDER_ID}.npm"
    if gateway_base_url is not None:
        options = broke.get("options")
        if not isinstance(options, dict) or options.get("baseURL") != gateway_base_url:
            return f"provider.{GATEWAY_PROVIDER_ID}.options.baseURL"
    models = broke.get("models")
    prefix = f"{GATEWAY_PROVIDER_ID}/"
    ids = {a.removeprefix(prefix) for a in allowed}
    if not isinstance(models, dict) or any(k not in ids for k in models):
        return f"provider.{GATEWAY_PROVIDER_ID}.models"
    small = body.get("small_model")
    if not (isinstance(small, str) and small in allowed):
        return "small_model"
    return _bad_model_key(body, allowed)


async def check_server_config(
    endpoint: ServerEndpoint,
    allowed: frozenset[str],
    *,
    gateway_base_url: str | None,
    timeout_s: float,
    transport: httpx.AsyncBaseTransport | None = None,
    environ: Mapping[str, str] | None = None,
) -> tuple[SmallModelCheck, str]:
    """Never raises. Returns (OK|UNSAFE|UNAVAILABLE, detail naming key paths only)."""
    problem = await asyncio.to_thread(auth_store_problem, environ)
    if problem is not None:
        return SmallModelCheck.UNSAFE, problem
    url = endpoint.url.rstrip("/") + "/config"
    try:
        async with httpx.AsyncClient(
            transport=transport,
            timeout=timeout_s,
            follow_redirects=False,
            trust_env=False,
            auth=endpoint.auth(),
        ) as client:
            response = await client.get(url)
        if response.status_code != 200:
            return SmallModelCheck.UNAVAILABLE, "GET /config failed"
        body = response.json()
    except Exception:
        return SmallModelCheck.UNAVAILABLE, "GET /config failed"
    if not isinstance(body, dict):
        return SmallModelCheck.UNAVAILABLE, "GET /config failed"
    bad = _gateway_shape_problem(body, allowed, gateway_base_url)
    if bad is not None:
        return SmallModelCheck.UNSAFE, f"server config key '{bad}' is not gateway-only"
    return SmallModelCheck.OK, ""


# ---- on-disk project config (B13) ---------------------------------------------------------


def strip_jsonc(text: str) -> str:
    """Remove // and /* */ comments and trailing commas outside of strings."""
    out: list[str] = []
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c == '"':
            j = i + 1
            while j < n and text[j] != '"':
                j += 2 if text[j] == "\\" else 1
            out.append(text[i : j + 1])
            i = j + 1
        elif text.startswith("//", i):
            while i < n and text[i] not in "\r\n":
                i += 1
        elif text.startswith("/*", i):
            end = text.find("*/", i + 2)
            i = n if end < 0 else end + 2
            out.append(" ")
        elif c == ",":
            j = i + 1
            while j < n:
                if text[j] in " \t\r\n":
                    j += 1
                elif text.startswith("//", j):
                    while j < n and text[j] not in "\r\n":
                        j += 1
                elif text.startswith("/*", j):
                    end = text.find("*/", j + 2)
                    j = n if end < 0 else end + 2
                else:
                    break
            if not (j < n and text[j] in "]}"):
                out.append(c)
            i += 1
        else:
            out.append(c)
            i += 1
    return "".join(out)


def _scope_dirs(project_dir: Path) -> list[Path]:
    """Project dir and every parent up to the filesystem root (OpenCode may merge any of them)."""
    start = project_dir.resolve()
    return [start, *start.parents]


def _candidate_files(project_dir: Path) -> list[Path]:
    files: list[Path] = []
    for d in _scope_dirs(project_dir):
        files += [d / "opencode.json", d / "opencode.jsonc"]
        oc = d / ".opencode"
        if oc.is_dir():
            files += sorted(p for p in oc.iterdir() if p.suffix in (".json", ".jsonc"))
    return files


def check_project_config(project_dir: str, allowed: frozenset[str]) -> tuple[SmallModelCheck, str]:
    """Fail-closed local check; returns (OK|UNSAFE, detail naming file and key on UNSAFE)."""
    try:
        files = _candidate_files(Path(project_dir))
    except OSError:
        return SmallModelCheck.UNSAFE, "project directory unreadable"
    for path in files:
        try:
            if not path.exists():
                continue
            data = json.loads(strip_jsonc(path.read_text(encoding="utf-8-sig")))
        except (OSError, ValueError):
            return SmallModelCheck.UNSAFE, f"{path}: unreadable or not valid JSON"
        if not isinstance(data, dict):
            return SmallModelCheck.UNSAFE, f"{path}: top level is not an object"
        for forbidden in _PROJECT_FORBIDDEN_KEYS:
            if forbidden in data:
                return (
                    SmallModelCheck.UNSAFE,
                    f"{path}: key '{forbidden}' is not allowed in project config",
                )
        bad = _bad_model_key(data, allowed)
        if bad is not None:
            return SmallModelCheck.UNSAFE, f"{path}: key '{bad}' is not an allowlisted model"
    return SmallModelCheck.OK, ""


async def check_project_config_async(
    project_dir: str, allowed: frozenset[str]
) -> tuple[SmallModelCheck, str]:
    return await asyncio.to_thread(check_project_config, project_dir, allowed)
