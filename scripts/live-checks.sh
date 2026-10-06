#!/usr/bin/env bash
set -euo pipefail
# Read-only live checks of the OpenCode user isolation (automated subset of TASKS.md T082
# L0-L11). Run it as the daemon user, not as root, from a shell WITHOUT secrets.
# It changes nothing: it only reads state and runs the four sudo-allowed helper commands as the
# OpenCode user.

REPO_DIR=$(cd "$(dirname "$0")/.." && pwd -P)
PY="$REPO_DIR/.venv/bin/python"
CONFIG="$REPO_DIR/config.yaml"

usage() {
    cat <<'USAGE'
Usage: scripts/live-checks.sh [--config PATH]

Read-only checks of the OpenCode user isolation (TASKS.md T082 L0-L11). Prints [PASS], [FAIL],
[SKIP], [MANUAL] and [INFO] lines and a summary. Run it as the daemon user (not root) from a
shell that has no DISCORD_BOT_TOKEN or provider keys set.

Exit codes: 0 no check failed, 1 at least one check failed, 2 refused or usage error.
USAGE
}

while [ $# -gt 0 ]; do
    case "$1" in
        -h | --help)
            usage
            exit 0
            ;;
        --config)
            [ $# -ge 2 ] || {
                usage >&2
                exit 2
            }
            CONFIG=$2
            shift 2
            ;;
        *)
            printf 'live-checks: unknown argument: %s\n' "$1" >&2
            usage >&2
            exit 2
            ;;
    esac
done

if [ "$(id -u)" -eq 0 ]; then
    echo "live-checks: refused: run it as the daemon user, not root" >&2
    exit 2
fi
present=""
for name in DISCORD_BOT_TOKEN OPENROUTER_API_KEY GEMINI_API_KEY GROQ_API_KEY; do
    if [ -n "${!name:-}" ]; then
        present="$present $name"
    fi
done
if [ -n "$present" ]; then
    printf 'live-checks: refused: these variables are set in this shell:%s\n' "$present" >&2
    echo "live-checks: run it from a shell without secrets" >&2
    exit 2
fi
if [ ! -x "$PY" ]; then
    printf 'live-checks: refused: %s is not executable\n' "$PY" >&2
    exit 2
fi

cd "$REPO_DIR"

PASSES=0
FAILS=0
SKIPS=0
MANUALS=0
pass() {
    printf '[PASS] %s\n' "$*"
    PASSES=$((PASSES + 1))
}
fail() {
    printf '[FAIL] %s\n' "$*"
    FAILS=$((FAILS + 1))
}
skip() {
    printf '[SKIP] %s\n' "$*"
    SKIPS=$((SKIPS + 1))
}
manual() {
    printf '[MANUAL] %s\n' "$*"
    MANUALS=$((MANUALS + 1))
}
info() { printf '[INFO] %s\n' "$*"; }

OS_USER_MODE=""
OC_USER=""
OPENCODE_BIN=""
SERVER_DIRECTORY=""
WORKING_DIRECTORY=""
DATABASE_PATH=""
MANAGED_SERVER=""
if ! values=$("$PY" -m app.ops.setup_values --config "$CONFIG"); then
    echo "live-checks: refused: cannot read the config values from $CONFIG" >&2
    exit 2
fi
while IFS='=' read -r key val; do
    case "$key" in
        OS_USER_MODE) OS_USER_MODE=$val ;;
        OC_USER) OC_USER=$val ;;
        OPENCODE_BIN) OPENCODE_BIN=$val ;;
        SERVER_DIRECTORY) SERVER_DIRECTORY=$val ;;
        WORKING_DIRECTORY) WORKING_DIRECTORY=$val ;;
        DATABASE_PATH) DATABASE_PATH=$val ;;
        MANAGED_SERVER) MANAGED_SERVER=$val ;;
    esac
done <<EOF_VALUES
$values
EOF_VALUES
if [ "$OS_USER_MODE" != "sudo" ]; then
    printf 'live-checks: refused: opencode.os_user.mode is %s, expected sudo\n' "$OS_USER_MODE" >&2
    exit 2
fi
: "$MANAGED_SERVER"

# Runs a command as the OpenCode user. It never sees a terminal.
as_oc() { setsid -w sudo -n -u "$OC_USER" -- "$@" </dev/null; }
# Same, but discards the output and stores the exit status in RC.
RC=0
as_oc_rc() {
    set +e
    as_oc "$@" >/dev/null 2>&1
    RC=$?
    set -e
}

SUDO_OK=1
need_sudo() { # need_sudo LABEL: skip the check if the sudo rule does not work
    if [ "$SUDO_OK" -eq 0 ]; then
        skip "$1 (sudo rule not working)"
        return 1
    fi
    return 0
}

# Daemon state from the PID file.
STATE_DIR="${XDG_STATE_HOME:-$HOME/.local/state}/broke-ai-coder"
DAEMON_PID=""
if [ -f "$STATE_DIR/daemon.pid" ]; then
    cand=$(cat "$STATE_DIR/daemon.pid" 2>/dev/null || true)
    case "$cand" in
        '' | *[!0-9]*) ;;
        *)
            if kill -0 "$cand" 2>/dev/null &&
                tr '\0' ' ' <"/proc/$cand/cmdline" 2>/dev/null | grep -q -- '-m app'; then
                DAEMON_PID=$cand
            fi
            ;;
    esac
fi
if [ -n "$DAEMON_PID" ]; then
    info "daemon is running (pid $DAEMON_PID)"
else
    info "daemon is not running"
fi

# ---- L0: host prerequisites ---------------------------------------------------------------------
if grep -qi microsoft /proc/version 2>/dev/null; then
    pass "L0 running inside WSL"
else
    fail "L0 not running inside WSL"
fi
pass "L0 virtualenv python present"

if [ -f "$OPENCODE_BIN" ]; then
    bin_stat=$(stat -L -c '%U %a' "$OPENCODE_BIN")
    bin_owner=${bin_stat% *}
    bin_mode=$(printf '%03d' "${bin_stat#* }")
    if [ "$bin_owner" = "root" ] && [ $((${bin_mode: -2:1} & 2)) -eq 0 ] && [ $((${bin_mode: -1} & 2)) -eq 0 ]; then
        pass "L0 $OPENCODE_BIN is root-owned and not group/other-writable"
    else
        fail "L0 $OPENCODE_BIN must be root-owned and not group/other-writable (docs D1)"
    fi
else
    fail "L0 $OPENCODE_BIN is not a regular file (docs D1)"
fi

if command -v git >/dev/null 2>&1; then
    git_version=$(git --version | awk '{print $3}')
    if [ "$(printf '%s\n%s\n' 2.45.1 "$git_version" | sort -V | head -n 1)" = "2.45.1" ]; then
        pass "L0 git $git_version is at least 2.45.1"
    else
        fail "L0 git $git_version is older than 2.45.1: add ppa:git-core/ppa, then apt update and upgrade git (docs Part F)"
    fi
else
    fail "L0 git is not installed"
fi

set +e
wsl_changes=$("$PY" -m app.ops.wslconf check /etc/wsl.conf)
wsl_rc=$?
set -e
if [ "$wsl_rc" -eq 0 ]; then
    pass "L0 /etc/wsl.conf has interop disabled and automount umask 077"
else
    fail "L0 /etc/wsl.conf is not compliant: $(printf '%s' "$wsl_changes" | tr '\n' ';')"
fi

# ---- L1: sudo rule ------------------------------------------------------------------------------
set +e
sudo -n true </dev/null >/dev/null 2>&1
rc=$?
set -e
if [ "$rc" -ne 0 ]; then
    pass "L1 the daemon user has no general passwordless sudo"
else
    fail "L1 the daemon user has passwordless sudo for everything"
fi

as_oc_rc "$OPENCODE_BIN" --version
if [ "$RC" -eq 0 ]; then
    pass "L1 the OpenCode binary runs as $OC_USER"
else
    fail "L1 the OpenCode binary does not run as $OC_USER (exit $RC)"
fi

as_oc_rc /usr/bin/test -d "/home/$OC_USER"
if [ "$RC" -eq 0 ]; then
    pass "L1 positive control: sudo -u $OC_USER works"
else
    SUDO_OK=0
    fail "L1 positive control failed (exit $RC): the sudoers rule is not working; the checks below are skipped"
fi
manual "L1 run 'sudo visudo -c' yourself and confirm it reports no errors"

# ---- L2 / L11: environment of the OpenCode user --------------------------------------------------
if need_sudo "L2 environment allowlist"; then
    set +e
    env_names=$(OPENCODE_SERVER_USERNAME=x FOO=y as_oc /usr/bin/head -c 4096 /proc/self/environ | tr '\0' '\n' | cut -d= -f1)
    set -e
    if printf '%s\n' "$env_names" | grep -qx OPENCODE_SERVER_USERNAME &&
        ! printf '%s\n' "$env_names" | grep -qx FOO; then
        pass "L2 env_keep passes OPENCODE_SERVER_USERNAME and drops other variables"
    else
        fail "L2 env_keep did not behave as expected (names seen: $(printf '%s' "$env_names" | tr '\n' ' '))"
    fi
    if printf '%s\n' "$env_names" | grep -qx XDG_DATA_HOME; then
        fail "L11 XDG_DATA_HOME is set for the OpenCode user"
    else
        pass "L11 XDG_DATA_HOME is not set for the OpenCode user"
    fi
fi

# ---- L3 / N1: serve processes --------------------------------------------------------------------
serve_pids=""
if [ -n "$DAEMON_PID" ]; then
    serve_pids=$(pgrep -u "$OC_USER" -f 'opencode serve' || true)
    if [ "$MANAGED_SERVER" != "true" ]; then
        skip "L3 serve is not managed by the daemon (opencode.managed_server: false)"
    elif [ -z "$serve_pids" ]; then
        fail "L3 the daemon is running but no opencode serve process of $OC_USER exists"
    elif need_sudo "L3 serve stdio"; then
        for pid in $serve_pids; do
            tty=$(ps -o tty= -p "$pid" | tr -d ' ')
            if [ "$tty" = "?" ]; then
                pass "L3 serve pid $pid has no controlling terminal"
            else
                fail "L3 serve pid $pid has terminal $tty"
            fi
            as_oc_rc /usr/bin/test -p "/proc/$pid/fd/1"
            if [ "$RC" -eq 0 ]; then
                pass "L3 serve pid $pid stdout is a pipe"
            else
                fail "L3 serve pid $pid stdout is not a pipe"
            fi
        done
    fi
else
    leftovers=$(pgrep -u "$OC_USER" || true)
    if [ -z "$leftovers" ]; then
        pass "N1 no processes of $OC_USER while the daemon is stopped"
    else
        fail "N1 processes of $OC_USER survive without the daemon: $(printf '%s' "$leftovers" | tr '\n' ' ')"
    fi
fi

# ---- L4: what the OpenCode user must not read ----------------------------------------------------
denied_read() { # denied_read LABEL PATH [x]: both a test and a read must be refused
    local label=$1 path=$2 flag=${3:--r}
    as_oc_rc /usr/bin/test "$flag" "$path"
    if [ "$RC" -eq 1 ]; then
        pass "L4 $label: not accessible for $OC_USER (test $flag)"
    else
        fail "L4 $label: test $flag returned $RC, expected 1"
    fi
    if [ "$flag" = "-r" ] && [ ! -d "$path" ]; then
        as_oc_rc /usr/bin/head -c 1 "$path"
        if [ "$RC" -ne 0 ]; then
            pass "L4 $label: cannot be read by $OC_USER"
        else
            fail "L4 $label: could be read by $OC_USER"
        fi
    fi
}
if need_sudo "L4 protected paths"; then
    secrets_file="$HOME/.config/broke-ai-coder/secrets.env"
    if [ -e "$secrets_file" ]; then
        denied_read "secrets file" "$secrets_file"
    else
        skip "L4 secrets file does not exist yet"
    fi
    denied_read "daemon home" "$HOME"
    denied_read "daemon home" "$HOME" -x
    if [ -e "$DATABASE_PATH" ]; then
        denied_read "database" "$DATABASE_PATH"
    else
        skip "L4 database file does not exist yet"
    fi
    if [ -d /mnt/c ]; then
        denied_read "/mnt/c" /mnt/c
    else
        skip "L4 /mnt/c is not mounted"
    fi
    if [ -n "$DAEMON_PID" ]; then
        denied_read "daemon /proc environ" "/proc/$DAEMON_PID/environ"
    else
        skip "L4 daemon environ (daemon not running)"
    fi
fi
manual "L4 as $OC_USER try a Windows .exe and 'sh -c' escapes (TASKS.md T082 L4)"

# ---- L5: working directory -----------------------------------------------------------------------
if [ -d "$WORKING_DIRECTORY" ]; then
    wd_stat=$(stat -c '%G %a' "$WORKING_DIRECTORY")
    if [ "$wd_stat" = "brokeai 2770" ]; then
        pass "L5 $WORKING_DIRECTORY is group brokeai, mode 2770"
    else
        fail "L5 $WORKING_DIRECTORY is '$wd_stat', expected 'brokeai 2770'"
    fi
    if getfacl -p "$WORKING_DIRECTORY" 2>/dev/null | grep -q '^default:group:brokeai:rwx'; then
        pass "L5 default ACL grants group brokeai rwx"
    else
        fail "L5 no default ACL for group brokeai on $WORKING_DIRECTORY"
    fi
else
    fail "L5 $WORKING_DIRECTORY does not exist (run scripts/wsl-setup.sh)"
fi
manual "L5 let the agent create a commit in a project and check the owner/group of the new files"

# ---- L6: OpenCode credential store ---------------------------------------------------------------
if need_sudo "L6 auth store"; then
    as_oc_rc /usr/bin/test -e "/home/$OC_USER/.local/share/opencode/auth.json"
    if [ "$RC" -eq 1 ]; then
        pass "L6 no OpenCode auth.json exists for $OC_USER"
    else
        fail "L6 auth.json exists for $OC_USER or the check failed (exit $RC)"
    fi
fi
manual "L6 plant a fake auth.json and confirm the daemon refuses to start (TASKS.md T082 L6)"

# ---- L7: WSL interop -----------------------------------------------------------------------------
if [ -r /proc/sys/fs/binfmt_misc/status ]; then
    interop_bad=0
    for f in /proc/sys/fs/binfmt_misc/WSLInterop*; do
        [ -e "$f" ] || continue
        if [ "$(head -n 1 "$f")" != "disabled" ]; then
            interop_bad=1
        fi
    done
    if [ "$interop_bad" -eq 0 ]; then
        pass "L7 every WSLInterop binfmt entry is disabled"
    else
        fail "L7 a WSLInterop binfmt entry is enabled (wsl --shutdown after fixing /etc/wsl.conf)"
    fi
else
    fail "L7 binfmt_misc is not mounted or not readable"
fi
if need_sudo "L7 interop sockets"; then
    for s in /run/WSL/*_interop; do
        [ -S "$s" ] || continue
        as_oc_rc /usr/bin/test -w "$s"
        if [ "$RC" -eq 1 ]; then
            pass "L7 $OC_USER cannot write to $s"
        else
            fail "L7 $OC_USER can write to $s (exit $RC)"
        fi
    done
fi
info "L7 legacy_tiocsti = $(cat /proc/sys/dev/tty/legacy_tiocsti 2>/dev/null || echo unavailable)"
manual "L7 as $OC_USER try /init and a .exe (both must fail)"

# ---- L8 / L9 / L10 -------------------------------------------------------------------------------
manual "L8 see TASKS.md T082 L8"
oc_shell=$(getent passwd "$OC_USER" | cut -d: -f7)
if [ "$oc_shell" = "/bin/bash" ]; then
    pass "L9 $OC_USER has /bin/bash as its shell"
else
    fail "L9 $OC_USER shell is '$oc_shell', expected /bin/bash"
fi
manual "L9 check that ~/.cache of $OC_USER is writable by the tools you use"

if [ -n "$DAEMON_PID" ] && [ "$MANAGED_SERVER" = "true" ]; then
    first_serve=${serve_pids%%$'\n'*}
    if [ -z "$first_serve" ]; then
        fail "L10 no serve process to inspect"
    elif need_sudo "L10 serve cwd"; then
        as_oc_rc /usr/bin/test "/proc/$first_serve/cwd" -ef "$SERVER_DIRECTORY"
        if [ "$RC" -eq 0 ]; then
            pass "L10 serve runs in $SERVER_DIRECTORY"
        else
            fail "L10 serve cwd is not $SERVER_DIRECTORY"
        fi
    fi
else
    skip "L10 serve cwd (daemon not running or serve not managed)"
fi
manual "L10 confirm the run cwd of a task is the project directory"

# ---- isolation self-check of the daemon itself ---------------------------------------------------
set +e
"$PY" -m app --check-os-user --config "$CONFIG" >/dev/null 2>&1
rc=$?
set -e
if [ "$rc" -eq 0 ]; then
    pass "check-os-user: the daemon's isolation self-check passed"
else
    fail "check-os-user: the daemon's isolation self-check failed (exit $rc); run it by hand for details"
fi

manual "L12 permission deny rules: see the TASKS.md T082 L12 prompts"
manual "L13 WSL boot persistence for the systemd unit: see TASKS.md T082 L13"

printf '\nSummary: %s passed, %s failed, %s skipped, %s manual\n' "$PASSES" "$FAILS" "$SKIPS" "$MANUALS"
if [ "$FAILS" -gt 0 ]; then
    exit 1
fi
exit 0
