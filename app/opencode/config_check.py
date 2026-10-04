"""Fail-closed checks of OpenCode's model-selecting config (FREE-ONLY safety).

B12. OpenCode makes background calls (e.g. title generation) with ``small_model``. When it is
     unset OpenCode picks a cheaper model from the provider itself or falls back to the main
     model, and there is no option to disable these calls. ``GET {server_url}/config`` returns
     the effective merged config object, which includes ``small_model`` ("provider/model")
     when set. The task only runs when ``small_model`` equals one of the allowlisted, enabled
     routing candidates in OpenCode form, which are free by construction. The same allowlist
     applies to ``model``, ``agent.*.model`` and ``mode.*.model`` when present.

B13. OpenCode merges configs (later wins): global ``~/.config/opencode/opencode.json``, the
     ``OPENCODE_CONFIG`` file, the project's ``opencode.json``, ``.opencode/`` directories, and
     ``OPENCODE_CONFIG_CONTENT``. ``GET /config`` has no documented per-request directory
     selector, so it reflects the server's own start directory only. Therefore, before each
     task, the project's on-disk config is checked too: ``opencode.json``/``opencode.jsonc``
     and ``*.json``/``*.jsonc`` directly in ``.opencode/``, in the project dir and each parent
     up to and including the git root (or only the project dir when no ``.git`` is found).
     Unparseable files are UNSAFE. The two env vars are stripped from the OpenCode child env.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import httpx

from app.config.models import AppConfig
from app.runtime.interfaces import SmallModelCheck

_FREE_PROVIDERS = frozenset({"openrouter", "google", "groq"})


def allowed_small_models(config: AppConfig) -> frozenset[str]:
    """Models of enabled policies on enabled, free-mapped providers as ``<oc provider>/<model>``."""
    pmap = config.opencode.provider_map
    out: set[str] = set()
    for pol in config.routing.policies.values():
        mapped = pmap.get(pol.provider)
        prov = config.providers.get(pol.provider)
        if not pol.enabled or prov is None or not prov.enabled or mapped not in _FREE_PROVIDERS:
            continue
        out.update(f"{mapped}/{model}" for model in pol.model_order)
    return frozenset(out)


def _bad_model_key(cfg: dict[str, Any], allowed: frozenset[str]) -> str | None:
    """Key path of the first non-allowlisted model value, or None.

    Checked: ``model``, ``small_model``, ``agent.<n>.model``, ``mode.<n>.model``. A value that
    is present but not an allowlisted string is bad (fail closed).
    """
    for key in ("model", "small_model"):
        if key in cfg and not (isinstance(cfg[key], str) and cfg[key] in allowed):
            return key
    for section in ("agent", "mode"):
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


async def check_small_model(
    server_url: str,
    allowed: frozenset[str],
    *,
    timeout_s: float,
    transport: httpx.AsyncBaseTransport | None = None,
) -> SmallModelCheck:
    """Never raises. Any failure to read the config is UNAVAILABLE; a bad value is UNSAFE."""
    url = server_url.rstrip("/") + "/config"
    try:
        async with httpx.AsyncClient(
            transport=transport, timeout=timeout_s, follow_redirects=False
        ) as client:
            response = await client.get(url)
        if response.status_code != 200:
            return SmallModelCheck.UNAVAILABLE
        body = response.json()
    except Exception:
        return SmallModelCheck.UNAVAILABLE
    if not isinstance(body, dict):
        return SmallModelCheck.UNAVAILABLE
    small = body.get("small_model")
    if not (isinstance(small, str) and small in allowed):
        return SmallModelCheck.UNSAFE
    if _bad_model_key(body, allowed) is not None:
        return SmallModelCheck.UNSAFE
    return SmallModelCheck.OK


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
    """Project dir and parents up to and including the git root; just the project dir if none."""
    start = project_dir.resolve()
    chain = [start, *start.parents]
    for idx, d in enumerate(chain):
        if (d / ".git").exists():
            return chain[: idx + 1]
    return [start]


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
        bad = _bad_model_key(data, allowed)
        if bad is not None:
            return SmallModelCheck.UNSAFE, f"{path}: key '{bad}' is not an allowlisted model"
    return SmallModelCheck.OK, ""


async def check_project_config_async(
    project_dir: str, allowed: frozenset[str]
) -> tuple[SmallModelCheck, str]:
    return await asyncio.to_thread(check_project_config, project_dir, allowed)
