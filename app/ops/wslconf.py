"""Line-based merge of /etc/wsl.conf with the settings OpenCode isolation needs (T072, W1).

Required: ``[interop] enabled=false`` and ``appendWindowsPath=false``, and an ``[automount]
options`` whose umask/fmask/dmask all mask group and other bits (``umask=077``). Comments,
unknown sections and unknown keys are preserved. Output uses LF line endings.

CLI: ``python -m app.ops.wslconf check PATH`` (exit 0 compliant, 1 not, 2 unreadable) and
``python -m app.ops.wslconf merge PATH`` (merged text on stdout; never writes files).
"""

from __future__ import annotations

import re
import sys
from collections.abc import Sequence

_SECTION_RE = re.compile(r"^\s*\[([^\]]*)\]\s*(?:[#;].*)?$")
_KEY_RE = re.compile(r"^(\s*)([A-Za-z0-9_.-]+)(\s*=\s*)(.*?)\s*$")
_MASKS = ("umask", "fmask", "dmask")
_INTEROP_KEYS = (("enabled", "false"), ("appendWindowsPath", "false"))


def _unquote(value: str) -> str:
    v = value.strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
        v = v[1:-1]
    return v.strip()


def _section_of(line: str) -> str | None:
    m = _SECTION_RE.match(line)
    return m.group(1).strip().lower() if m else None


def _key_of(line: str) -> tuple[str, str, str, str] | None:
    if line.lstrip().startswith(("#", ";")):
        return None
    m = _KEY_RE.match(line)
    return (m.group(1), m.group(2), m.group(3), m.group(4)) if m else None


def _first_section_span(lines: list[str], name: str) -> tuple[int, int] | None:
    """(header index, index after the last key line) of the first section ``name``."""
    start: int | None = None
    last_key = -1
    for i, line in enumerate(lines):
        sec = _section_of(line)
        if sec is not None:
            if start is not None:
                break
            if sec == name:
                start = i
                last_key = i
            continue
        if start is not None and _key_of(line) is not None:
            last_key = i
    if start is None:
        return None
    return start, last_key + 1


def _ensure_key(lines: list[str], section: str, key: str, value: str, changes: list[str]) -> None:
    """Fix a wrong value in every occurrence, else insert or append."""
    current: str | None = None
    found = False
    for i, line in enumerate(lines):
        sec = _section_of(line)
        if sec is not None:
            current = sec
            continue
        if current != section:
            continue
        parsed = _key_of(line)
        if parsed is None or parsed[1].lower() != key.lower():
            continue
        found = True
        if _unquote(parsed[3]).lower() != value:
            lines[i] = f"{parsed[0]}{parsed[1]}{parsed[2]}{value}"
            changes.append(f"[{section}] {parsed[1]} set to {value}")
    if found:
        return
    span = _first_section_span(lines, section)
    if span is None:
        if lines and lines[-1].strip():
            lines.append("")
        lines += [f"[{section}]", f"{key}={value}"]
    else:
        lines.insert(span[1], f"{key}={value}")
    changes.append(f"[{section}] {key}={value} added")


def _mask_ok(value: str) -> bool:
    try:
        return int(value, 8) & 0o077 == 0o077
    except ValueError:
        return False


def _fix_options(raw: str) -> str | None:
    """New options text, or None if already compliant."""
    items = [p.strip() for p in _unquote(raw).split(",") if p.strip()]
    out: list[str] = []
    changed = False
    have_umask = False
    for item in items:
        name, sep, val = item.partition("=")
        lname = name.strip().lower()
        if sep and lname in _MASKS:
            if lname == "umask":
                have_umask = True
            if not _mask_ok(val.strip()):
                out.append(f"{name.strip()}=077")
                changed = True
                continue
        out.append(item)
    if not have_umask:
        out.append("umask=077")
        changed = True
    return ",".join(out) if changed else None


def _ensure_automount(lines: list[str], changes: list[str]) -> None:
    current: str | None = None
    found = False
    for i, line in enumerate(lines):
        sec = _section_of(line)
        if sec is not None:
            current = sec
            continue
        if current != "automount":
            continue
        parsed = _key_of(line)
        if parsed is None or parsed[1].lower() != "options":
            continue
        found = True
        fixed = _fix_options(parsed[3])
        if fixed is not None:
            lines[i] = f'{parsed[0]}{parsed[1]}{parsed[2]}"{fixed}"'
            changes.append("[automount] options masked to umask=077")
    if found:
        return
    span = _first_section_span(lines, "automount")
    if span is None:
        if lines and lines[-1].strip():
            lines.append("")
        lines += ["[automount]", 'options="umask=077"']
    else:
        lines.insert(span[1], 'options="umask=077"')
    changes.append('[automount] options="umask=077" added')


def merge_wsl_conf(text: str) -> tuple[str, list[str]]:
    """Return (new text, human-readable changes); no changes means already compliant."""
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    changes: list[str] = []
    for key, value in _INTEROP_KEYS:
        _ensure_key(lines, "interop", key, value, changes)
    _ensure_automount(lines, changes)
    return "\n".join(lines) + "\n", changes


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 2 or args[0] not in ("check", "merge"):
        print("usage: python -m app.ops.wslconf (check|merge) PATH", file=sys.stderr)
        return 2
    mode, path = args
    try:
        with open(path, encoding="utf-8") as f:
            text = f.read()
    except FileNotFoundError:
        text = ""
    except (OSError, UnicodeDecodeError):
        print(f"cannot read {path}", file=sys.stderr)
        return 2
    merged, changes = merge_wsl_conf(text)
    if mode == "merge":
        sys.stdout.write(merged)
        return 0
    for c in changes:
        print(c)
    return 1 if changes else 0


if __name__ == "__main__":
    raise SystemExit(main())
