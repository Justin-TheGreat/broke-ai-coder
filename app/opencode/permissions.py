"""OpenCode permission deny list (T060 minimal).

The daemon injects a ``permission`` block into the inline OpenCode config: ``bash`` commands that
publish, bulk-delete, deploy, change the environment or touch credentials are DENIED, and so are
``external_directory`` access, ``websearch`` and ``doom_loop``. Before each task the effective
config (``GET /config``) must still show those denies (``app/opencode/config_check.py``).

This is a **guardrail, not a security boundary**. Bypasses exist: wrapper scripts (``bash x.sh``),
interpreters, plugins, project agent markdown and ``git -C dir push``. The boundary is the OS user
(no credentials, no sudo, no secrets; T072). Deny is stricter than approval: Discord approval
buttons (T061) are future work. The built-in list cannot be removed; config can only add patterns
(``opencode.permission.extra_bash_deny``).

Assumptions (verify at T082 L12):

P1. The installed OpenCode accepts ``permission`` with ``bash`` (a pattern object),
    ``external_directory``, ``websearch`` and ``doom_loop``. Serve starts with no schema error.
P2. Bash patterns are matched against the text of each individual command after OpenCode splits a
    compound line (``&&``, ``;``, ``|``). ``*`` matches any characters including spaces, and the
    last matching rule wins. Pipelines such as ``curl | sh`` are therefore not caught (residual).
P3. ``GET /config`` returns ``permission`` as merged: object form, key order preserved, string
    values. If not, every task fails closed with OPENCODE_UNSAFE_CONFIG and the check needs a
    code change.
P4. ``external_directory`` is evaluated against the session's project directory (the run cwd),
    not serve's cwd.
P5. A denied call in headless ``opencode run`` comes back to the model as a tool error. There is
    no prompt and no hang, and the run continues.
P6. A per-agent ``permission`` is merged over the global one, which is why agent-level non-deny
    bash entries are rejected.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from app.config.models import OpenCodePermissionConfig

DENY = "deny"
ALLOW = "allow"
DENIED_TOOLS: tuple[str, ...] = ("external_directory", "websearch", "doom_loop")

DEFAULT_BASH_DENY: tuple[str, ...] = (
    # publication / push
    "*git push*",
    "gh auth*",
    "gh repo delete*",
    "gh release*",
    "docker push*",
    "npm publish*",
    "pnpm publish*",
    "yarn publish*",
    "yarn npm publish*",
    "cargo publish*",
    "twine upload*",
    "gem push*",
    "poetry publish*",
    "uv publish*",
    # bulk delete
    "rm -r*",
    "rm -R*",
    "rm -fr*",
    "rm -fR*",
    "rm -f -r*",
    "rm --recursive*",
    "rm * -r*",
    "rm * -R*",
    "rm * -fr*",
    "rm * -fR*",
    "rm * --recursive*",
    "find * -delete*",
    "git clean*",
    "git rm -r*",
    "shred *",
    # deploy / production
    "kubectl *",
    "helm *",
    "terraform apply*",
    "terraform destroy*",
    "pulumi up*",
    "pulumi destroy*",
    "vercel*",
    "netlify deploy*",
    "fly deploy*",
    "flyctl deploy*",
    "firebase deploy*",
    "heroku *",
    "serverless deploy*",
    "sls deploy*",
    "gcloud *",
    "aws *",
    "az *",
    "ansible-playbook*",
    "ssh *",
    "scp *",
    # environment-changing installs
    "sudo*",
    "su *",
    "apt *",
    "apt-get *",
    "aptitude *",
    "dpkg -i*",
    "snap *",
    "brew *",
    "npm install -g*",
    "npm install --global*",
    "npm i -g*",
    "npm i --global*",
    "npm -g *",
    "pnpm add -g*",
    "pnpm add --global*",
    "pnpm install -g*",
    "yarn global*",
    "pip install --user*",
    "pip3 install --user*",
    "python -m pip install --user*",
    "python3 -m pip install --user*",
    "*--break-system-packages*",
    "pipx *",
    "uv tool install*",
    "cargo install*",
    "go install*",
    "gem install*",
    # credentials
    "git config --global*",
    "git config --system*",
    "git credential*",
    "opencode auth*",
    "ssh-keygen*",
    "ssh-add*",
    "ssh-copy-id*",
    "npm login*",
    "npm adduser*",
    "npm token*",
    "docker login*",
    "passwd*",
    "chpasswd*",
)


def bash_deny_patterns(cfg: OpenCodePermissionConfig) -> tuple[str, ...]:
    """DEFAULT_BASH_DENY followed by cfg.extra_bash_deny; duplicates dropped, first kept."""
    seen: dict[str, None] = {}
    for p in (*DEFAULT_BASH_DENY, *cfg.extra_bash_deny):
        seen.setdefault(p, None)
    return tuple(seen)


def build_permission_block(cfg: OpenCodePermissionConfig) -> dict[str, Any]:
    """The ``permission`` object. ``"*"`` is the FIRST bash key (last match wins)."""
    bash: dict[str, str] = {"*": ALLOW}
    for p in bash_deny_patterns(cfg):
        bash[p] = DENY
    block: dict[str, Any] = {"bash": bash}
    for tool in DENIED_TOOLS:
        block[tool] = DENY
    return block


def default_permission_block() -> dict[str, Any]:
    from app.config.models import OpenCodePermissionConfig

    return build_permission_block(OpenCodePermissionConfig())


def permission_problem(body: Mapping[str, Any], required: Mapping[str, Any]) -> str | None:
    """Key path of the first problem in a merged OpenCode config, or None.

    Never includes pattern text.
    """
    perm = body.get("permission")
    if not isinstance(perm, dict):
        return "permission"
    for k in DENIED_TOOLS:
        if k in required and perm.get(k) != DENY:
            return f"permission.{k}"
    bash = perm.get("bash")
    if not isinstance(bash, dict):
        return "permission.bash"
    for index, (key, value) in enumerate(bash.items()):
        if index == 0 and key == "*":
            if not isinstance(value, str):
                return "permission.bash"
            continue
        if value != DENY:
            return "permission.bash"
    req_bash = required.get("bash")
    if isinstance(req_bash, Mapping):
        for p in req_bash:
            if p != "*" and bash.get(p) != DENY:
                return "permission.bash"
    for section in ("agent", "mode"):
        bad = agent_permission_problem(section, body.get(section))
        if bad is not None:
            return bad
    return None


def agent_permission_problem(section: str, entries: Any) -> str | None:
    """Key path of an agent/mode ``permission`` that could weaken the deny rules, or None."""
    if not isinstance(entries, dict):
        return None
    for name, entry in entries.items():
        if not isinstance(entry, dict) or "permission" not in entry:
            continue
        p = entry["permission"]
        base = f"{section}.{name}.permission"
        if not isinstance(p, dict):
            return base
        for k in DENIED_TOOLS:
            if k in p and p[k] != DENY:
                return f"{base}.{k}"
        if "bash" in p:
            b = p["bash"]
            if b == DENY:
                continue
            if isinstance(b, dict) and all(v == DENY for v in b.values()):
                continue
            return f"{base}.bash"
    return None
