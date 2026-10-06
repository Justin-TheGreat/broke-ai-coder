"""wsl-setup.sh path guards (R-1) and the hardening round, run for real in bash.

The guard functions are cut out of the script and executed with the filesystem helpers
(`canon_path`, `path_kind`, `owner_mode`) stubbed, so nothing here touches a real path.
"""

from __future__ import annotations

import re

import pytest

from tests.test_scripts import BASH, SCRIPTS, needs_bash, run_bash, text

ROOT = SCRIPTS.parent


def guard_block() -> str:
    body = text("wsl-setup.sh")
    return body[body.index("# ---- path guards") : body.index("# ---- Phase A")]


STUBS = r"""
declare -A KIND OM LINKS
KIND[/]=dir
canon_path() {
    local p=$1 out=() part k IFS=/
    for part in $p; do
        case "$part" in
            "" | .) ;;
            ..) [ ${#out[@]} -eq 0 ] || unset 'out[-1]' ;;
            *) out+=("$part") ;;
        esac
    done
    p="/${out[*]}"
    for k in "${!LINKS[@]}"; do
        case "$p" in
            "$k") p=${LINKS[$k]} ;;
            "$k"/*) p=${LINKS[$k]}${p#"$k"} ;;
        esac
    done
    printf '%s\n' "$p"
}
path_kind() { echo "${KIND[$1]:-missing}"; }
owner_mode() { echo "${OM[$1]:-root 755}"; }
problem() { printf 'PROBLEM: %s\n' "$*"; }
"""


def run_guard(call: str, setup: str = "") -> list[str]:
    script = "set -eu\n" + guard_block() + STUBS + setup + "\n" + call + "\n"
    r = run_bash("-c", script)
    assert r.returncode == 0, r.stderr
    return [ln for ln in r.stdout.splitlines() if ln.startswith("PROBLEM:")]


@needs_bash
@pytest.mark.parametrize(
    "path",
    [
        "/tmp/../home/jichen",
        "/var/lib/../../etc",
        "/usr/local",
        "/",
        "/etc",
        "/usr",
        "/root",
        "/home",
        "/home/jichen",
        "/home/jichen/work",
        "/bin",
        "/sbin/x",
        "/lib64",
        "/boot",
        "/dev/shm",
        "/proc/1",
        "/sys",
        "/run/user",
        "/snap",
        "/tmp",
        "/tmp/work",
        "/var/tmp/work",
        "/mnt/c/Users",
        "/media/x",
        "/opt",
        "/opt/x",
        "/srv",
        "/var",
        "/var/lib",
        "/var/log",
        "/var/lib/other",
        "/var/lib/broke-ai-coder",
        "relative/dir",
        "/workspace/",
        "/workspace/.",
        "//workspace",
        "/workspace//sub",
        "/workspace/../etc",
    ],
)
def test_guard_rejects_path(path):
    assert len(run_guard(f'check_root_dir WORKING_DIRECTORY "{path}"')) == 1, path


@needs_bash
@pytest.mark.parametrize(
    "path",
    [
        "/workspace",
        "/workspace/project",
        "/var/lib/broke-ai-coder/opencode-server",
        "/data/work",
        "/srvx",
        "/homeless",
        "/usrlocal",
    ],
)
def test_guard_accepts_default_and_ordinary_paths(path):
    assert run_guard(f'check_root_dir SERVER_DIRECTORY "{path}"') == [], path


@needs_bash
def test_guard_rejects_symlinks_and_non_directories():
    setup = "KIND[/workspace]=symlink; LINKS[/workspace]=/workspace\n"
    out = run_guard("check_root_dir WORKING_DIRECTORY /workspace", setup)
    assert len(out) == 1 and "symlink" in out[0]
    # a symlinked component: realpath -m resolves it to somewhere else
    out = run_guard(
        "check_root_dir SERVER_DIRECTORY /data/link/srv", "LINKS[/data/link]=/etc/real\n"
    )
    assert len(out) == 1 and "normalized" in out[0]
    # a symlink whose target is elsewhere is refused as non-canonical
    setup = "KIND[/data/srv]=symlink; LINKS[/data/srv]=/etc\n"
    assert len(run_guard("check_root_dir SERVER_DIRECTORY /data/srv", setup)) == 1
    out = run_guard("check_root_dir WORKING_DIRECTORY /workspace", "KIND[/workspace]=other\n")
    assert len(out) == 1 and "not a directory" in out[0]


@needs_bash
@pytest.mark.parametrize(
    "om", ["jichen 755", "root 775", "root 757", "root 777", "root 1777", "root 770", "root 702"]
)
def test_guard_rejects_unsafe_nearest_ancestor(om):
    setup = f'KIND[/data]=dir; OM[/data]="{om}"\n'
    assert len(run_guard("check_root_dir WORKING_DIRECTORY /data/ws", setup)) == 1


@needs_bash
def test_guard_checks_every_existing_ancestor():
    ok = 'KIND[/data]=dir; OM[/data]="root 755"; KIND[/data/a]=dir; OM[/data/a]="root 755"\n'
    assert run_guard("check_root_dir WORKING_DIRECTORY /data/a/ws", ok) == []
    bad = 'KIND[/data]=dir; OM[/data]="jichen 777"; KIND[/data/a]=dir; OM[/data/a]="root 755"\n'
    assert len(run_guard("check_root_dir WORKING_DIRECTORY /data/a/ws", bad)) == 1
    # a missing parent is skipped; / itself is checked
    assert len(run_guard("check_root_dir WORKING_DIRECTORY /ws", 'OM[/]="root 777"\n')) == 1
    assert run_guard("check_root_dir WORKING_DIRECTORY /ws") == []


@needs_bash
@pytest.mark.parametrize(
    "a,b",
    [
        ("/workspace/.serve", "/workspace"),
        ("/workspace", "/workspace/.serve"),
        ("/workspace", "/workspace"),
        ("/home/jichen/broke-ai-coder/data", "/home/jichen"),
        ("/var/lib/broke-ai-coder", "/var/lib/broke-ai-coder/db"),
    ],
)
def test_guard_apart_rejects_equal_or_nested_paths(a, b):
    assert len(run_guard(f'check_apart SERVER_DIRECTORY "{a}" WORKING_DIRECTORY "{b}"')) == 1


@needs_bash
def test_guard_apart_accepts_siblings_and_skips_empty():
    call = 'check_apart A /workspace B /workspace2; check_apart A /workspace B ""'
    assert run_guard(call) == []


def test_guard_denylist_is_the_agreed_one():
    block = guard_block()
    line = next(ln for ln in block.splitlines() if ln.startswith("FORBIDDEN_TREES="))
    trees = line.split('"')[1].split()
    expected = (
        "/bin /boot /dev /etc /lib /lib32 /lib64 /libx32 /opt /proc /root /run /sbin /snap "
        "/srv /sys /usr /tmp /var/tmp /mnt /home /media"
    ).split()
    assert sorted(trees) == sorted(expected)
    assert "realpath -m" in block and "OWN_VAR_TREE=/var/lib/broke-ai-coder" in block


def test_guards_run_in_phase_a_before_any_change():
    body = text("wsl-setup.sh")
    lines = body.splitlines()
    gate = next(i for i, ln in enumerate(lines) if ln.strip() == 'if [ "$ERRORS" -gt 0 ]; then')
    start = next(i for i, ln in enumerate(lines) if ln.startswith("# ---- Phase A"))
    phase_a = "\n".join(lines[start:gate])
    for call in ("check_root_dir SERVER_DIRECTORY", "check_root_dir WORKING_DIRECTORY"):
        assert call in phase_a
    # $H is validated (and USER_OK cleared) before step 4 can chmod it
    assert 'valid_home "$U" "$H" || USER_OK=0' in phase_a
    assert "must be an absolute, normalized, existing directory" in phase_a
    assert body.index("must be an absolute, normalized, existing directory") < body.index(
        'run chmod 700 "$H"'
    )
    assert "realpath" in next(
        ln for ln in phase_a.splitlines() if ln.strip().startswith("for c in")
    )


def test_wsl_setup_recommended_hardening():
    body = text("wsl-setup.sh")
    # sudoers: dotted temp name, validated there, then renamed; "up to date" checks owner and mode
    assert "SUDOERS_NEW=/etc/sudoers.d/.broke-ai-coder.new" in body
    fn = body[body.index("install_sudoers() {") :]
    fn = fn[: fn.index("\n}\n")]
    assert fn.index('"$SUDOERS_NEW"') < fn.index('visudo -cf "$SUDOERS_NEW"') < fn.index("mv -f")
    assert '"$(owner_mode "$SUDOERS_DEST")" = "root 440"' in body
    # an empty merged wsl.conf is refused before the install
    assert body.index('[ -z "$merged" ]') < body.index('"$WSLCONF_TMP" /etc/wsl.conf')
    # the ERR trap names the failing step; every Phase B step sets STEP
    assert "set -o errtrace" in body and "trap 'on_err $LINENO' ERR" in body
    assert body.count('\nSTEP="') >= 10
    # git runs from /, keeps its exit status and its result is verified
    git = body[body.index("oc_git() {") : body.index('if [ -e "$OC_GITCONFIG" ]')]
    assert "(cd / &&" in git and "| cat" not in git and re.search(r"\|\|\s+die ", git)
    assert '[ -e "$OC_GITCONFIG" ] || die' in body
    assert "| cat" not in body
    assert "sudo apt install acl" in body
    # the OpenCode user's home comes from one checked variable, not a hard-coded path in Phase B
    assert 'oc_home_pw" != "$OC_HOME"' in body
    assert 'chmod 700 "/home/$OC_USER"' not in body


def test_live_checks_positive_sudo_control_drops_cached_credentials():
    body = text("live-checks.sh")
    assert "sudo -n -k true" in body
    assert "sudo -n true" not in body
    assert "docs setup guide C2" in body


def test_start_daemon_makes_a_relative_config_absolute_before_cd():
    body = text("start-daemon.sh")
    fix = body.index('*) CONFIG="$PWD/$CONFIG"')
    assert fix < body.index('[ -f "$CONFIG" ]') < body.index('cd "$REPO_DIR"')


@needs_bash
def test_start_daemon_relative_config_is_resolved_against_the_caller_directory(tmp_path):
    import subprocess

    (tmp_path / "mine.yaml").write_text("x: 1\n", encoding="utf-8")
    r = subprocess.run(
        [BASH, str(SCRIPTS / "start-daemon.sh"), "--config", "nope.yaml"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=60,
        stdin=subprocess.DEVNULL,
    )
    assert r.returncode == 2
    assert "refused" in r.stderr


def test_docs_readme_and_tasks_follow_the_review():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "#part-a--discord-15-min-from-a-pc-browser" in readme
    assert "#part-b--provider-api-keys-15-min" in readme
    assert "acl" in readme and "git-core" in readme
    guide = (ROOT / "docs" / "SETUP-discord-opencode.md").read_text(encoding="utf-8")
    apt = next(ln for ln in guide.splitlines() if ln.startswith("sudo apt install -y git curl"))
    assert apt.split()[-1] == "acl"
    assert "ppa:git-core/ppa" in guide.split("### C3.")[0]
    assert "network-online.target" in guide and "L13" in guide
    tasks = (ROOT / "TASKS.md").read_text(encoding="utf-8")
    t072 = tasks.split("## T072 ")[1].split("**Status:**")[1].splitlines()[0].strip()
    assert t072 == "DONE (offline; live check in T082)"
    assert "network-online.target" in tasks.split("- L13 (T102)")[1].splitlines()[0]
    assert re.search(r"(?m)^### C2\. Packages", guide)


@needs_bash
@pytest.mark.parametrize(
    "home,setup,ok",
    [
        ("/home/jichen", 'KIND[/home/jichen]=dir; OM[/home/jichen]="jichen 750"', True),
        ("/", 'OM[/]="jichen 755"', False),
        ("", "", False),
        ("home/jichen", 'KIND[home/jichen]=dir; OM[home/jichen]="jichen 750"', False),
        ("/home/jichen", "", False),  # does not exist
        ("/home/jichen", 'KIND[/home/jichen]=symlink; OM[/home/jichen]="jichen 750"', False),
        ("/home/jichen", 'KIND[/home/jichen]=dir; OM[/home/jichen]="root 750"', False),
        (
            "/home/x/../jichen",
            'KIND[/home/x/../jichen]=dir; OM[/home/x/../jichen]="jichen 750"',
            False,
        ),
        ("/usr", 'KIND[/usr]=dir; OM[/usr]="root 755"', False),
    ],
)
def test_valid_home_guards_the_chmod_700_target(home, setup, ok):
    call = f'if valid_home jichen "{home}"; then echo VALID; else echo INVALID; fi'
    script = "set -eu\n" + guard_block() + STUBS + setup + "\n" + call + "\n"
    r = run_bash("-c", script)
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == ("VALID" if ok else "INVALID")


def containment_block() -> str:
    body = text("wsl-setup.sh")
    start = body.index('    check_apart SERVER_DIRECTORY "$SERVER_DIRECTORY" WORKING_DIRECTORY')
    end = body.index("    done\n", start) + len("    done\n")
    return body[start:end]


def run_containment(**v: str) -> list[str]:
    env = {
        "SERVER_DIRECTORY": "/var/lib/broke-ai-coder/opencode-server",
        "WORKING_DIRECTORY": "/workspace",
        "REPO_DIR": "/data/repo",
        "DATABASE_PATH": "/data/repo/data/app.db",
        "H": "/data/me",
        "OC_HOME": "/data/oc",
        "USER_OK": "1",
    }
    env.update(v)
    assigns = "".join(f'{k}="{val}"\n' for k, val in env.items())
    # the block starts with the server-vs-working check and recomputes DB_DIR itself
    script = "set -eu\n" + guard_block() + STUBS + assigns + containment_block()
    r = run_bash("-c", script)
    assert r.returncode == 0, r.stderr
    return [ln for ln in r.stdout.splitlines() if ln.startswith("PROBLEM:")]


@needs_bash
def test_containment_defaults_pass():
    assert run_containment() == []


@needs_bash
@pytest.mark.parametrize(
    "var,val",
    [
        ("WORKING_DIRECTORY", "/var/lib/broke-ai-coder/opencode-server/w"),  # inside server
        ("WORKING_DIRECTORY", "/var/lib/broke-ai-coder/opencode-server"),  # equal to server
        ("SERVER_DIRECTORY", "/workspace/.serve"),  # inside working
        ("WORKING_DIRECTORY", "/data/repo"),  # equals repo
        ("WORKING_DIRECTORY", "/data/repo/sub"),  # inside repo
        ("WORKING_DIRECTORY", "/data"),  # contains repo
        ("SERVER_DIRECTORY", "/data/repo/srv"),
        ("WORKING_DIRECTORY", "/data/repo/data"),  # equals DB dir
        ("WORKING_DIRECTORY", "/data/repo/data/x"),  # inside DB dir
        ("WORKING_DIRECTORY", "/data/me"),  # equals daemon home
        ("WORKING_DIRECTORY", "/data/me/w"),  # inside daemon home
        ("SERVER_DIRECTORY", "/data"),  # contains daemon home
        ("WORKING_DIRECTORY", "/data/oc"),  # equals OpenCode home
        ("SERVER_DIRECTORY", "/data/oc/x"),  # inside OpenCode home
    ],
)
def test_containment_rejects(var, val):
    assert run_containment(**{var: val}), (var, val)


@needs_bash
def test_containment_database_dir_containing_a_path_and_home_skip():
    # the database directory contains the serve directory
    out = run_containment(DATABASE_PATH="/var/lib/broke-ai-coder/app.db")
    assert any("database directory" in ln for ln in out)
    # your home is only compared when the daemon user was valid
    assert run_containment(WORKING_DIRECTORY="/data/me/w", USER_OK="0") == []


def _gh_anchor(heading: str) -> str:
    s = heading.strip().lower()
    s = re.sub(r"[^\w\- ]", "", s)  # GitHub drops punctuation (including the approx sign)
    return s.replace(" ", "-")


def test_readme_guide_anchors_match_computed_github_anchors():
    guide = (ROOT / "docs" / "SETUP-discord-opencode.md").read_text(encoding="utf-8")
    anchors: set[str] = set()
    in_code = False
    for ln in guide.splitlines():
        if ln.startswith("```"):
            in_code = not in_code
        elif not in_code and re.match(r"#{1,6} ", ln):
            anchors.add(_gh_anchor(ln.lstrip("#")))
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    links = re.findall(r"docs/SETUP-discord-opencode\.md#([\w\-]+)", readme)
    assert "part-a--discord-15-min-from-a-pc-browser" in links
    assert "part-b--provider-api-keys-15-min" in links
    assert [a for a in links if a not in anchors] == []


@needs_bash
@pytest.mark.parametrize(
    "step,expect",
    [("Phase A: checks", "nothing was changed"), ("Phase B: step 3", "re-running is safe")],
)
def test_err_trap_message_names_line_and_step(step, expect):
    body = text("wsl-setup.sh")
    fn = body[
        body.index("on_err() {") : body.index("trap 'on_err $LINENO' ERR")
        + len("trap 'on_err $LINENO' ERR")
    ]
    script = f'set -eEu\nSTEP="{step}"\n{fn}\nfalse\n'
    r = run_bash("-c", script)
    assert r.returncode != 0
    assert "stopped at line" in r.stderr and step in r.stderr and expect in r.stderr
