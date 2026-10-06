"""Operations scripts, systemd unit and secrets template (static checks only)."""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

from app.config.loader import load_config

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
SH = ["start-daemon.sh", "stop-daemon.sh", "health-check.sh", "wsl-setup.sh", "live-checks.sh"]
UNIT = SCRIPTS / "systemd" / "broke-ai-coder.service"


def find_bash() -> str | None:
    found = shutil.which("bash")
    if found and not any(bad in found.lower() for bad in ("system32", "windowsapps")):
        return found
    git_bash = Path(r"C:\Program Files\Git\bin\bash.exe")
    return str(git_bash) if git_bash.exists() else None


BASH = find_bash()
needs_bash = pytest.mark.skipif(BASH is None, reason="no POSIX bash available")


def text(name: str) -> str:
    return (SCRIPTS / name).read_text(encoding="utf-8")


def run_bash(*args: str) -> subprocess.CompletedProcess[str]:
    assert BASH is not None
    return subprocess.run(
        [BASH, *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=60,
        stdin=subprocess.DEVNULL,
    )


def test_files_exist():
    for name in SH:
        assert (SCRIPTS / name).is_file(), name
    assert (SCRIPTS / "bench_idle.py").is_file()
    assert UNIT.is_file()


@pytest.mark.parametrize("name", SH)
def test_shell_script_basics(name):
    raw = (SCRIPTS / name).read_bytes()
    assert b"\r\n" not in raw
    lines = raw.decode("utf-8").splitlines()
    assert lines[0] == "#!/usr/bin/env bash"
    assert any(ln.startswith("set -eu") for ln in lines[:15])


def test_git_index_mode_is_executable():
    if shutil.which("git") is None:
        pytest.skip("git not available")
    out = subprocess.run(
        ["git", "ls-files", "-s", "--", "scripts"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if out.returncode != 0:
        pytest.skip("not a git checkout")
    modes = {}
    for line in out.stdout.splitlines():
        meta, path = line.split("\t", 1)
        modes[path] = meta.split()[0]
    for name in SH + ["bench_idle.py"]:
        assert modes.get(f"scripts/{name}") == "100755", name


@needs_bash
@pytest.mark.parametrize("name", SH)
def test_bash_syntax(name):
    r = run_bash("-n", f"scripts/{name}")
    assert r.returncode == 0, r.stderr


@pytest.mark.skipif(shutil.which("shellcheck") is None, reason="shellcheck not installed")
@pytest.mark.parametrize("name", SH)
def test_shellcheck(name):
    r = subprocess.run(
        ["shellcheck", "-S", "warning", f"scripts/{name}"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert r.returncode == 0, r.stdout


@needs_bash
def test_wsl_setup_without_yes_refuses():
    r = run_bash("scripts/wsl-setup.sh")
    assert r.returncode == 2
    assert "--yes" in r.stdout + r.stderr


@needs_bash
@pytest.mark.parametrize("name", SH)
def test_help_exits_zero_and_unknown_argument_exits_two(name):
    assert run_bash(f"scripts/{name}", "--help").returncode == 0
    r = run_bash(f"scripts/{name}", "--definitely-not-an-option")
    assert r.returncode == 2


FORBIDDEN_IN_LIVE_CHECKS = (
    "sudo -S",
    "read -rs",
    "rm -",
    "chmod",
    "chown",
    "setfacl -m",
    "useradd",
    "usermod",
    "install -",
    "tee ",
    "mkfifo",
    "> /etc",
)


@pytest.mark.parametrize("token", FORBIDDEN_IN_LIVE_CHECKS)
def test_live_checks_is_read_only(token):
    assert token not in text("live-checks.sh")


def test_live_checks_has_positive_sudo_control_and_no_tty():
    body = text("live-checks.sh")
    assert "as_oc /usr/bin/test -d" in body or "as_oc_rc /usr/bin/test -d" in body
    assert "SUDO_OK=0" in body and "sudo rule not working" in body
    assert "setsid -w sudo -n -u" in body and "</dev/null" in body


def test_wsl_setup_never_touches_secrets_content_or_runs_python_as_root():
    body = text("wsl-setup.sh")
    assert "source" not in body
    assert '. "$' not in body
    assert not re.search(r"\bcat\b[^\n]*secrets\.env", body)
    for line in body.splitlines():
        if '"$PY" -m' in line and not line.lstrip().startswith("#"):
            assert "runuser -u" in line, line


def test_start_daemon_execs_python_module():
    assert 'exec "$PY" -m app' in text("start-daemon.sh")


def test_unit_file_rules():
    unit = UNIT.read_text(encoding="utf-8")
    assert b"\r\n" not in UNIT.read_bytes()
    assert "User=@USER@" in unit
    assert "RestartPreventExitStatus=2 3" in unit
    assert "NoNewPrivileges=yes" not in unit
    assert not re.search(r"(?m)^\s*NoNewPrivileges=", unit)
    assert not re.search(r"(?m)^\s*EnvironmentFile=", unit)


def test_secrets_example_matches_config_names():
    lines = (ROOT / "secrets.env.example").read_text(encoding="utf-8").splitlines()
    names = set()
    for ln in lines:
        if not ln.strip() or ln.startswith("#"):
            continue
        assert re.match(r"^export [A-Z_][A-Z0-9_]*=$", ln), ln
        names.add(ln[len("export ") : -1])
    cfg = load_config(ROOT / "config.example.yaml")
    expected = {p.api_key_env for p in cfg.providers.values()} | {cfg.discord.bot_token_env}
    assert names == expected


def test_gitattributes_pins_lf():
    ga = (ROOT / ".gitattributes").read_text(encoding="utf-8")
    for rule in ("*.sh text eol=lf", "scripts/systemd/* text eol=lf", "secrets.env.example"):
        assert rule in ga


# ---- audit additions: structural checks that do not rely on literal substrings ---------------

HEREDOC_START = re.compile(r"<<-?\s*['\"]?(\w+)['\"]?")
MUTATING_COMMANDS = (
    "useradd usermod userdel groupadd groupdel gpasswd chpasswd passwd chmod chown chgrp "
    "setfacl install tee mkfifo mkdir rmdir rm mv cp dd truncate ln touch mount umount "
    "sed ed patch visudo crontab systemctl service apt apt-get dpkg pip npm"
).split()


def code_only(raw: str) -> str:
    """Script text without comments, heredoc bodies and quoted string contents."""
    out: list[str] = []
    end: str | None = None
    for line in raw.splitlines():
        if end is not None:
            if line.strip() == end:
                end = None
            continue
        stripped = "" if line.lstrip().startswith("#") else line
        m = HEREDOC_START.search(stripped)
        if m:
            end = m.group(1)
            stripped = stripped[: m.start()]
        stripped = re.sub(r"'[^']*'", "''", stripped)
        stripped = re.sub(r'"[^"]*"', '""', stripped)
        stripped = re.sub(r"\s#.*$", "", stripped)
        out.append(stripped)
    return "\n".join(out)


def command_words(code: str) -> set[str]:
    found = set()
    for cmd in MUTATING_COMMANDS:
        if re.search(
            rf"(?<![\w.\-])({re.escape(cmd)})(?![\w\-])", code.replace("getent passwd", "")
        ):
            found.add(cmd)
    return found


def read_only_violations(raw: str) -> list[str]:
    """Names of anything in a shell script that could change the system."""
    code = code_only(raw)
    code = code.replace("command -v git", "").replace("git --version", "")
    found = sorted(command_words(code))
    if re.search(r"\bsed\b[^\n]*\s-[a-zA-Z]*i", code):
        found.append("sed -i")
    for m in re.finditer(r"(\d*)(>>?|>\|)\s*(&\d|\S+)", code):
        target = m.group(3).rstrip(";)")
        if target != "/dev/null" and not target.startswith("&"):
            found.append(f"redirect to {target}")
    if re.search(r"\bsudo\b(?!\s+-n\b)", code):
        found.append("sudo without -n")
    if re.search(r"\bgit\s+\w", code):
        found.append("git")
    return found


def test_read_only_scanner_catches_mutations():
    base = "#!/usr/bin/env bash\nset -eu\necho hi >/dev/null 2>&1\nsudo -n true\n"
    assert read_only_violations(base) == []
    for evil, label in (
        ("useradd evil", "useradd"),
        ("chmod 777 /etc/sudoers", "chmod"),
        ("echo x | tee /etc/passwd", "tee"),
        ("echo x > /etc/sudoers.d/x", "redirect"),
        ("echo x >> /usr/local/bin/y", "redirect"),
        ("echo x >/etc/wsl.conf", "redirect"),
        ("cp a /etc/b", "cp"),
        ("sed -i s/a/b/ /etc/c", "sed"),
        ("/usr/bin/install -m 0440 a b", "install"),
        ("sudo useradd x", "useradd"),
        ("sudo id", "sudo without -n"),
        ("setfacl -m u:x:rwx /d", "setfacl"),
        ("rm -rf /x", "rm"),
        ("git push", "git"),
    ):
        got = read_only_violations(base + evil + "\n")
        assert any(label in g for g in got), (evil, got)
    harmless = (
        base + "# chmod is documented\nprintf 'run chmod 700 > /etc/x'\ncat <<'U'\nuseradd\nU\n"
    )
    assert read_only_violations(harmless) == []


def test_live_checks_is_read_only_structurally():
    assert read_only_violations(text("live-checks.sh")) == []


def test_live_checks_as_oc_only_runs_the_sudoers_allowed_helpers():
    code = code_only(text("live-checks.sh"))
    calls = [
        ln.strip()
        for ln in code.splitlines()
        if re.search(r"\bas_oc(_rc)?\b", ln) and not re.search(r"as_oc(_rc)?\(\)", ln)
    ]
    assert calls
    for ln in calls:
        assert re.search(r"as_oc(_rc)?\s+(\"\"|/usr/bin/(test|head))", ln) or (
            "setsid -w sudo -n -u" in ln
        ), ln


def test_live_checks_positive_control_runs_before_every_denial_check():
    lines = text("live-checks.sh").splitlines()
    control = next(
        i for i, ln in enumerate(lines) if ln.strip().startswith("as_oc_rc /usr/bin/test -d")
    )
    gate = next(i for i, ln in enumerate(lines) if ln.strip() == "SUDO_OK=0")
    assert control < gate < control + 6  # set only when the control fails
    assert 'if [ "$RC" -eq 0 ]; then' in lines[control + 1]
    assert lines[gate - 1].strip() == "else"
    # every as_oc/denied_read call after the control sits behind a need_sudo guard, so a
    # missing sudoers rule skips (never passes) a denial check
    guarded = 0
    helper = _first_index(lines, lambda ln: ln.startswith("denied_read() {"))
    helper_end = helper + _first_index(lines[helper:], lambda ln: ln == "}")
    for i, ln in enumerate(lines):
        if i <= control or ln.lstrip().startswith("#") or helper <= i <= helper_end:
            continue
        if re.match(r"\s*(as_oc_rc|denied_read)\s", ln) or re.search(r"=\$?\(?.*\bas_oc\b", ln):
            assert "need_sudo" in "\n".join(lines[max(control, i - 30) : i]), (i + 1, ln)
            guarded += 1
    assert guarded >= 8
    # the definitions of denial helpers sit before the control but are only CALLED after it
    calls = [i for i, ln in enumerate(lines) if re.match(r"\s+denied_read\s+\"", ln)]
    assert calls and min(calls) > control


def test_need_sudo_skips_instead_of_passing():
    body = text("live-checks.sh")
    m = re.search(r"need_sudo\(\) \{.*?\n\}", body, re.S)
    assert m
    fn = m.group(0)
    assert 'skip "$1 (sudo rule not working)"' in fn and "return 1" in fn
    assert "pass" not in fn.replace("passwordless", "")


def test_live_checks_refuses_root_and_secret_variables_and_prints_names_only():
    body = text("live-checks.sh")
    assert 'if [ "$(id -u)" -eq 0 ]; then' in body
    for name in ("DISCORD_BOT_TOKEN", "OPENROUTER_API_KEY", "GEMINI_API_KEY", "GROQ_API_KEY"):
        assert name in body
    assert "${!name:-}" in body
    assert not re.search(r"(echo|printf)[^\n]*\$\{!name", body)


def _first_index(lines: list[str], pred) -> int:
    return next(i for i, ln in enumerate(lines) if pred(ln))


def test_wsl_setup_yes_gate_precedes_every_other_action():
    lines = text("wsl-setup.sh").splitlines()
    gate = _first_index(lines, lambda ln: ln.strip() == 'if [ "$YES" -ne 1 ]; then')
    assert any("exit 2" in ln for ln in lines[gate : gate + 5])
    before = code_only("\n".join(lines[:gate]))
    assert command_words(before) == set()
    for needle in ("id -u", "getent", "runuser", "SUDO_USER"):
        assert needle not in before


def test_wsl_setup_phase_a_checks_everything_and_changes_nothing_before_exiting():
    body = text("wsl-setup.sh")
    lines = body.splitlines()
    phase_b = _first_index(lines, lambda ln: ln.startswith("# ---- Phase B"))
    exit_gate = _first_index(lines, lambda ln: ln.strip() == 'if [ "$ERRORS" -gt 0 ]; then')
    assert exit_gate < phase_b
    assert "exit 2" in "\n".join(lines[exit_gate : exit_gate + 4])
    phase_a_start = _first_index(lines, lambda ln: ln.startswith("# ---- Phase A"))
    phase_a = "\n".join(lines[phase_a_start:exit_gate])
    cmd_line = next(ln for ln in phase_a.splitlines() if ln.strip().startswith("for c in"))
    code = "\n".join(
        ln for ln in code_only(phase_a).splitlines() if not ln.lstrip().startswith("for c in")
    )
    assert command_words(code) - {"visudo", "install"} == set()
    # the only file Phase A creates is the sudoers render in /root, removed by the EXIT trap
    assert code.count("mktemp") == 1 and "trap cleanup EXIT" in body
    for needle in (
        '"$(id -u)" -ne 0',
        "SUDO_USER",
        '"$U" = "root"',
        "getent passwd",
        "microsoft",
        "/workspace",
        "--print-sudoers",
        '"($OC_USER)"',
        "app.ops.setup_values",
        '"$OS_USER_MODE" = "sudo"',
        "stat -L -c '%U %a'",
        "root | sudo | admin | wheel | adm | docker | lxd | disk | shadow",
        "secrets.env",
        'oc_uid" -eq 0',
        'check_root_dir SERVER_DIRECTORY "$SERVER_DIRECTORY"',
        'check_root_dir WORKING_DIRECTORY "$WORKING_DIRECTORY"',
        'check_apart SERVER_DIRECTORY "$SERVER_DIRECTORY" WORKING_DIRECTORY',
        '"the repository" "$REPO_DIR"',
        '"the database directory" "$DB_DIR"',
        '"your home" "$H"',
        '"the OpenCode user\'s home" "$OC_HOME"',
    ):
        assert needle in phase_a, needle
    wanted = "getent groupadd useradd usermod gpasswd setfacl getfacl visudo install runuser"
    wanted += " setsid stat id mktemp cmp"
    for c in wanted.split():
        assert re.search(rf"\b{c}\b", cmd_line), c
    assert "sudo apt install acl" in phase_a


def test_wsl_setup_actions_are_idempotent_and_backups_come_first():
    body = text("wsl-setup.sh")
    assert re.search(r"if getent group brokeai[^\n]*\n[^\n]*ok [^\n]*\nelse\n\s*run groupadd", body)
    assert re.search(r'if in_group "\$U" brokeai; then[^\n]*\n[^\n]*\nelse\n\s*run usermod', body)
    assert re.search(r'if getent passwd "\$OC_USER"[^\n]*\n[^\n]*\nelse\n\s*run useradd', body)
    assert re.search(
        r'if \[ -e "\$SECRETS" \]; then\n[^\n]*\nelse\n\s*run runuser[^\n]*touch', body
    )
    assert re.search(r'if \[ -e "\$OC_GITCONFIG" \]; then\n[^\n]*not touched', body)
    # /etc/wsl.conf: only on a reported change, backup before install
    backup = body.index('run cp -p /etc/wsl.conf "/etc/wsl.conf.bak-broke-ai-coder-')
    install = body.index('run install -m 0644 -o root -g root "$WSLCONF_TMP" /etc/wsl.conf')
    assert backup < install
    assert '0) ok "/etc/wsl.conf already' in body
    # sudoers: validate, skip when identical, back up before install, roll back on failure
    assert body.index("run visudo -cf") < body.index("cmp -s")
    assert body.index('run cp -p "$SUDOERS_DEST" "$BACKUP"') < body.index(
        'install_sudoers "$SUDOERS_TMP"'
    )
    rollback = body[body.index('if ! install_sudoers "$SUDOERS_TMP"') :]
    assert 'run rm -f "$SUDOERS_DEST"' in rollback and "exit 1" in rollback
    assert 'install_sudoers "$BACKUP"' in rollback
    # the working directory is never changed recursively
    for ln in body.splitlines():
        if ln.strip().startswith(("run chown", "run chmod", "run setfacl")):
            assert not re.search(r"\s-R\b|\s-[a-zA-Z]*R", ln), ln
    assert not re.search(r"\bapt(-get)?\s+install\b", code_only(body))


def test_wsl_setup_never_handles_keys_or_tokens():
    body = text("wsl-setup.sh")
    for needle in (
        "DISCORD_BOT_TOKEN",
        "OPENROUTER_API_KEY",
        "GEMINI_API_KEY",
        "GROQ_API_KEY",
        "read -s",
        "read -rs",
        "sk-",
        "eval ",
    ):
        assert needle not in body, needle
    reader = r"\b(cat|grep|head|tail|sed|awk|cut|source|read|more|less|od|xxd|strings|cp|mv)\b"
    for ln in body.splitlines():
        if ln.lstrip().startswith("#") or not re.search(r"SECRETS|secrets\.env", ln):
            continue
        assert not re.search(reader + r"[^\n]*(SECRETS|secrets\.env)", ln), ln
        assert not re.search(r"(SECRETS|secrets\.env)[^\n]*(<|(?<!\|)\|(?!\|))", ln), ln
    for ln in body.splitlines():
        if ln.lstrip().startswith("#"):
            continue
        if re.search(r"(^|[\s(])(python3?|\"\$PY\")\s+-m\b", ln):
            assert 'runuser -u "$U" -- "$PY"' in ln, ln


def test_wsl_setup_yes_with_unusable_sudo_user_refuses_and_changes_nothing():
    if BASH is None:
        pytest.skip("no POSIX bash available")
    import os

    for sudo_user in ("", "root"):
        r = subprocess.run(
            [BASH, "scripts/wsl-setup.sh", "--yes"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=60,
            stdin=subprocess.DEVNULL,
            env={**os.environ, "SUDO_USER": sudo_user},
        )
        out = r.stdout + r.stderr
        assert r.returncode == 2, (sudo_user, out)
        assert "nothing was changed" in out and "SUDO_USER" in out
        assert "[setup] RUN:" not in out  # no action was started
    r = run_bash("scripts/wsl-setup.sh", "--yes", "--bogus")
    assert r.returncode == 2 and "[setup] RUN:" not in r.stdout


def test_start_daemon_loads_secrets_only_into_the_daemon_process():
    body = text("start-daemon.sh")
    lines = [ln for ln in body.splitlines() if ln.strip()]
    assert any(ln.startswith("set -eu") for ln in lines[:5])
    assert lines[-1] == 'exec "$PY" -m app --config "$CONFIG"'
    code = code_only(body)
    assert not re.search(r"\bexport\b|\beval\b|\bdeclare -x\b", code)
    set_a = body.index("set -a\n")
    source = body.index('. "$SECRETS"')
    set_off = body.index("set +a\n")
    assert set_a < source < set_off < body.index("exec ")
    # loaded after every refusal and the pid handling
    assert body.rindex("refuse ") < set_a and body.index("already running") < set_a
    # the file is never printed; the malformed-line check prints line numbers only
    grep = next(ln for ln in body.splitlines() if "grep -nvE" in ln)
    assert "cut -d: -f1" in grep and " -o" not in grep
    for ln in code.splitlines():
        if "SECRETS" in ln:
            assert not re.search(r"\b(cat|head|tail|less|more|echo|printf|tee)\b", ln), ln
    for guard in ('[ ! -L "$SECRETS" ]', "stat -c %u", "stat -c %a", "600 | 400"):
        assert guard in body, guard
    assert '[ "$(id -u)" -ne 0 ]' in body


def test_secrets_example_has_names_only_and_no_value_shaped_text():
    raw = (ROOT / "secrets.env.example").read_text(encoding="utf-8")
    for ln in raw.splitlines():
        if ln.startswith("export "):
            assert ln.endswith("=") and ln.count("=") == 1, ln
    assert not re.search(r"sk-|AIza|ghp_|xox[bp]-|[A-Za-z0-9]{30,}", raw)


def test_tasks_md_status_sync_and_single_oc_function_line():
    raw = (ROOT / "TASKS.md").read_text(encoding="utf-8")
    assert raw.count("OC() {") == 1
    parts = re.split(r"(?m)^## (T\d+A?) ", raw)[1:]
    statuses: dict[str, list[str]] = {}
    for tid, rest in zip(parts[0::2], parts[1::2], strict=True):
        statuses[tid] = re.findall(r"(?m)^\*\*Status:\*\*\s*(.*)$", rest)
    assert all(len(v) == 1 for v in statuses.values()), {
        k: v for k, v in statuses.items() if len(v) != 1
    }
    done = (
        "T001 T001A T011 T012 T013 T020 T021 T021A T022 T023 T030 T031 T034 T035 T040 T041 T042 "
        "T043 T050 T051 T051A T052 T053 T054 T055 T063 T070 T071 T073 T080 T081 T090 T091 T092 "
        "T094 T100 T101 T103"
    ).split()
    for tid in done:
        assert statuses[tid][0] == "DONE", tid
    for tid in ("T003", "T032"):
        assert statuses[tid][0] == "DONE (offline; live check in T082)", tid
    for tid in "T010 T024 T044 T060 T084 T093 T102".split():
        assert statuses[tid][0].startswith("PARTIAL:"), tid
    for tid in "T002 T061 T082 T083".split():
        assert statuses[tid][0].startswith("TODO"), tid
    for tid in "T110 T111 T112 T114".split():
        assert statuses[tid][0] == "BACKLOG", tid


def test_tasks_md_claims_match_code():
    for rel in (
        "app/gateway/server.py",
        "app/opencode/permissions.py",
        "app/runtime/resources.py",
        "app/runtime/bench.py",
        "app/ops/wslconf.py",
        "scripts/bench_idle.py",
        "scripts/systemd/broke-ai-coder.service",
        "secrets.env.example",
    ):
        assert (ROOT / rel).is_file(), rel
    commands = (ROOT / "app/bot/commands.py").read_text(encoding="utf-8")
    names = re.search(r"COMMAND_NAMES = \(([^)]*)\)", commands)
    assert names and "approve" not in names.group(1) and "deny" not in names.group(1)
    orch = (ROOT / "app/orchestrator/orchestrator.py").read_text(encoding="utf-8")
    assert "provider_refresh_interval_s" in orch  # T044: lazy, rate-limited refresh


def test_need_sudo_is_driven_by_the_positive_control_flag():
    body = text("live-checks.sh")
    assert "SUDO_OK=1" in body.split("need_sudo()")[0].splitlines()[-1]
    fn = re.search(r"need_sudo[(][)] [{].*?[}]", body, re.S).group(0)
    assert 'if [ "$SUDO_OK" -eq 0 ]; then' in fn
    assert body.count("SUDO_OK=0") == 1
