#!/usr/bin/env bash
set -eu
# One-time WSL setup for broke-ai-coder (docs/SETUP-discord-opencode.md C5, C6 steps 1-5, E2).
# Run it yourself:  sudo scripts/wsl-setup.sh --yes
#
# It never reads, prints or prompts for keys or tokens, never loads the secrets file, never
# installs packages, and never runs repository Python as root (Python helpers run through
# `runuser -u <you>`). Every action is printed before it runs, and every step is idempotent.

REPO_DIR=$(cd "$(dirname "$0")/.." && pwd -P)
PY="$REPO_DIR/.venv/bin/python"
CONFIG="$REPO_DIR/config.yaml"
YES=0

usage() {
    cat <<'USAGE'
Usage: sudo scripts/wsl-setup.sh --yes [--config PATH]

Makes the changes that need root, once, so OpenCode can run as a separate Linux user:
  - creates the group brokeai and the OpenCode user (opencode.os_user.name), no sudo rights
  - chmod 700 on your home and on the OpenCode user's home
  - creates ~/.config/broke-ai-coder/secrets.env (empty, mode 600; the content is never read)
  - prepares opencode.working_directory (setgid 2770, group brokeai, ACLs) and the serve directory
  - merges /etc/wsl.conf (interop off, automount umask 077; a backup is kept)
  - installs /etc/sudoers.d/broke-ai-coder after `visudo -cf` (a backup is kept)
  - sets the git identity of the OpenCode user if it has no ~/.gitconfig

Run it with sudo from your normal WSL user. Without --yes nothing is changed (exit 2).
Exit codes: 0 done, 1 a sudoers install failed and was rolled back, 2 refused or usage error.
USAGE
}

while [ $# -gt 0 ]; do
    case "$1" in
        -h | --help)
            usage
            exit 0
            ;;
        --yes)
            YES=1
            shift
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
            printf 'wsl-setup: unknown argument: %s\n' "$1" >&2
            usage >&2
            exit 2
            ;;
    esac
done

if [ "$YES" -ne 1 ]; then
    usage >&2
    echo >&2
    echo "wsl-setup: nothing was changed; re-run with --yes to apply the steps above." >&2
    exit 2
fi

case "$CONFIG" in
    /*) ;;
    *) CONFIG="$PWD/$CONFIG" ;;
esac

run() {
    printf '[setup] RUN: %s\n' "$*"
    "$@"
}
ok() { printf '[setup] OK: %s\n' "$*"; }
note() { printf '[setup] NOTE: %s\n' "$*"; }
fail() {
    printf '[setup] FAIL: %s\n' "$*" >&2
    exit 2
}

ERRORS=0
problem() {
    printf '[setup] PROBLEM: %s\n' "$*" >&2
    ERRORS=$((ERRORS + 1))
}

SUDOERS_TMP=""
WSLCONF_TMP=""
cleanup() {
    [ -z "$SUDOERS_TMP" ] || rm -f "$SUDOERS_TMP"
    [ -z "$WSLCONF_TMP" ] || rm -f "$WSLCONF_TMP"
}
trap cleanup EXIT

cd "$REPO_DIR"

# ---- Phase A: preconditions (nothing is changed until all of them hold) -------------------------

U=""
H=""
U_UID=""
OS_USER_MODE=""
OC_USER=""
OPENCODE_BIN=""
SERVER_DIRECTORY=""
WORKING_DIRECTORY=""
DATABASE_PATH=""
MANAGED_SERVER=""
USER_OK=0

if [ "$(id -u)" -ne 0 ]; then
    problem "must run as root: sudo scripts/wsl-setup.sh --yes"
fi
U="${SUDO_USER:-}"
if [ -z "$U" ] || [ "$U" = "root" ]; then
    problem "SUDO_USER is not set or is root: run it with sudo from your normal user"
elif ! getent passwd "$U" >/dev/null; then
    problem "user $U (SUDO_USER) does not exist"
else
    H=$(getent passwd "$U" | cut -d: -f6)
    U_UID=$(id -u "$U")
    USER_OK=1
fi

if ! grep -qi microsoft /proc/version 2>/dev/null; then
    problem "this is not WSL (/proc/version does not mention microsoft)"
fi

CMDS_OK=1
for c in getent groupadd useradd usermod gpasswd setfacl getfacl visudo install runuser setsid stat id mktemp cmp; do
    if ! command -v "$c" >/dev/null 2>&1; then
        CMDS_OK=0
        if [ "$c" = "setfacl" ] || [ "$c" = "getfacl" ]; then
            problem "command $c is missing: apt install -y acl"
        else
            problem "command $c is missing"
        fi
    fi
done

REPO_OK=1
if [ "$USER_OK" -eq 1 ] && [ "$(stat -c %U "$REPO_DIR")" != "$U" ]; then
    REPO_OK=0
    problem "$REPO_DIR is not owned by $U"
fi
case "$REPO_DIR" in
    /workspace | /workspace/*)
        REPO_OK=0
        problem "$REPO_DIR must not be under /workspace (the agent's working area)"
        ;;
esac
if [ ! -x "$PY" ]; then
    REPO_OK=0
    problem "$PY is not executable (create the venv, setup guide E1)"
fi

VALUES_OK=0
if [ "$USER_OK" -eq 1 ] && [ "$REPO_OK" -eq 1 ] && [ "$CMDS_OK" -eq 1 ]; then
    if values=$(runuser -u "$U" -- "$PY" -m app.ops.setup_values --config "$CONFIG"); then
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
        VALUES_OK=1
    else
        problem "could not read the config values from $CONFIG (see the error above)"
    fi
fi
: "$DATABASE_PATH" "$MANAGED_SERVER"

if [ "$VALUES_OK" -eq 1 ]; then
    [ "$OS_USER_MODE" = "sudo" ] || problem "opencode.os_user.mode is '$OS_USER_MODE', must be sudo"

    case "$OPENCODE_BIN" in
        /*) ;;
        *) problem "opencode.command[0] must be an absolute path (setup guide D1)" ;;
    esac
    if [ ! -f "$OPENCODE_BIN" ]; then
        problem "$OPENCODE_BIN is not a regular file: copy the opencode binary there (docs D1)"
    else
        bin_stat=$(stat -L -c '%U %a' "$OPENCODE_BIN")
        bin_owner=${bin_stat% *}
        bin_mode=${bin_stat#* }
        bin_mode=$(printf '%03d' "$bin_mode")
        bin_g=${bin_mode: -2:1}
        bin_o=${bin_mode: -1}
        if [ "$bin_owner" != "root" ] || [ $((bin_g & 2)) -ne 0 ] || [ $((bin_o & 2)) -ne 0 ]; then
            problem "$OPENCODE_BIN must be owned by root and not group/other-writable (docs D1)"
        fi
    fi

    for pair in "SERVER_DIRECTORY=$SERVER_DIRECTORY" "WORKING_DIRECTORY=$WORKING_DIRECTORY"; do
        name=${pair%%=*}
        path=${pair#*=}
        case "$path" in
            /*) ;;
            *) problem "$name '$path' must be an absolute path" ;;
        esac
        if [ "$USER_OK" -eq 1 ]; then
            case "$path" in
                "$H" | "$H"/*) problem "$name '$path' must not be under your home ($H)" ;;
            esac
        fi
    done

    if [ "$USER_OK" -eq 1 ] && [ "$CMDS_OK" -eq 1 ]; then
        SUDOERS_TMP=$(mktemp -p /root broke-ai-coder.sudoers.XXXXXX)
        if runuser -u "$U" -- "$PY" -m app --print-sudoers --config "$CONFIG" >"$SUDOERS_TMP"; then
            if [ ! -s "$SUDOERS_TMP" ] || ! grep -qF "($OC_USER)" "$SUDOERS_TMP"; then
                problem "the generated sudoers text is empty or does not name ($OC_USER)"
            fi
        else
            problem "python -m app --print-sudoers failed (see the error above)"
        fi
    fi

    if getent passwd "$OC_USER" >/dev/null; then
        oc_uid=$(id -u "$OC_USER")
        if [ "$oc_uid" -eq 0 ] || { [ "$USER_OK" -eq 1 ] && [ "$oc_uid" = "$U_UID" ]; }; then
            problem "user $OC_USER has uid $oc_uid, which is root or your own user"
        fi
        read -ra oc_groups <<<"$(id -nG "$OC_USER")"
        for g in "${oc_groups[@]}"; do
            case "$g" in
                root | sudo | admin | wheel | adm | docker | lxd | disk | shadow)
                    problem "$OC_USER is in the privileged group $g: run 'gpasswd -d $OC_USER $g'"
                    ;;
            esac
        done
    fi

    if [ -L "$WORKING_DIRECTORY" ]; then
        problem "working directory $WORKING_DIRECTORY is a symlink"
    elif [ -e "$WORKING_DIRECTORY" ] && [ ! -d "$WORKING_DIRECTORY" ]; then
        problem "working directory $WORKING_DIRECTORY exists and is not a directory"
    fi
fi

if [ "$USER_OK" -eq 1 ]; then
    SECRETS="$H/.config/broke-ai-coder/secrets.env"
    if [ -e "$SECRETS" ] || [ -L "$SECRETS" ]; then
        if [ -L "$SECRETS" ] || [ ! -f "$SECRETS" ]; then
            problem "$SECRETS is not a regular file (or is a symlink)"
        elif [ "$(stat -c %U "$SECRETS")" != "$U" ]; then
            problem "$SECRETS is not owned by $U"
        fi
    fi
fi

if [ "$ERRORS" -gt 0 ]; then
    printf '[setup] %s problem(s) found; nothing was changed.\n' "$ERRORS" >&2
    exit 2
fi
ok "all preconditions hold (user $U, OpenCode user $OC_USER)"

# ---- Phase B: actions ---------------------------------------------------------------------------

CHANGED=""
NEED_SHUTDOWN=0
GROUP_CHANGED=0
changed() { CHANGED="$CHANGED
  - $*"; }
in_group() { # in_group USER GROUP
    case " $(id -nG "$1") " in
        *" $2 "*) return 0 ;;
    esac
    return 1
}

# 1-2. group brokeai and your membership
if getent group brokeai >/dev/null; then
    ok "group brokeai exists"
else
    run groupadd brokeai
    changed "created group brokeai"
fi
if in_group "$U" brokeai; then
    ok "$U is in group brokeai"
else
    run usermod -aG brokeai "$U"
    GROUP_CHANGED=1
    changed "added $U to group brokeai (log in again for it to apply)"
fi

# 3. the OpenCode user
if getent passwd "$OC_USER" >/dev/null; then
    ok "user $OC_USER exists"
else
    run useradd --create-home --home-dir "/home/$OC_USER" --shell /bin/bash --user-group "$OC_USER"
    changed "created user $OC_USER"
fi
if in_group "$OC_USER" brokeai; then
    ok "$OC_USER is in group brokeai"
else
    run usermod -aG brokeai "$OC_USER"
    changed "added $OC_USER to group brokeai"
fi
run chmod 700 "/home/$OC_USER"

# 4. your home
printf '[setup] NOTE: mode of %s was %s\n' "$H" "$(stat -c %a "$H")"
run chmod 700 "$H"

# 5. secrets directory and file (the content is never read)
CFG_DIR="$H/.config/broke-ai-coder"
SECRETS="$CFG_DIR/secrets.env"
run runuser -u "$U" -- mkdir -p "$CFG_DIR"
run chmod 700 "$CFG_DIR"
if [ -e "$SECRETS" ]; then
    ok "secrets file exists (not read)"
else
    run runuser -u "$U" -- touch "$SECRETS"
    changed "created the empty secrets file $SECRETS"
fi
run chmod 600 "$SECRETS"

# 6. working directory (non-recursive)
if [ ! -d "$WORKING_DIRECTORY" ]; then
    run install -d -m 2770 -o "$U" -g brokeai "$WORKING_DIRECTORY"
    changed "created $WORKING_DIRECTORY"
else
    run chown "$U:brokeai" "$WORKING_DIRECTORY"
    run chmod 2770 "$WORKING_DIRECTORY"
    if [ -n "$(ls -A "$WORKING_DIRECTORY")" ]; then
        note "$WORKING_DIRECTORY is not empty: existing content is not touched; see docs C5"
    fi
fi
run setfacl -m g:brokeai:rwX "$WORKING_DIRECTORY"
run setfacl -d -m g:brokeai:rwX "$WORKING_DIRECTORY"

# 7. serve directory
SERVER_PARENT=$(dirname "$SERVER_DIRECTORY")
if [ -d "$SERVER_PARENT" ]; then
    ok "$SERVER_PARENT exists"
else
    run install -d -m 0755 -o root -g root "$SERVER_PARENT"
    changed "created $SERVER_PARENT"
fi
if [ -d "$SERVER_DIRECTORY" ] && [ "$(stat -c '%U:%G %a' "$SERVER_DIRECTORY")" = "$OC_USER:brokeai 2770" ]; then
    ok "$SERVER_DIRECTORY is $OC_USER:brokeai 2770"
else
    run install -d -m 2770 -o "$OC_USER" -g brokeai "$SERVER_DIRECTORY"
    changed "prepared $SERVER_DIRECTORY"
fi

# 8. /etc/wsl.conf
set +e
WSL_CHANGES=$(runuser -u "$U" -- "$PY" -m app.ops.wslconf check /etc/wsl.conf)
WSL_RC=$?
set -e
case "$WSL_RC" in
    0) ok "/etc/wsl.conf already has interop disabled and automount umask 077" ;;
    1)
        printf '[setup] /etc/wsl.conf needs these changes:\n%s\n' "$WSL_CHANGES"
        merged=$(runuser -u "$U" -- "$PY" -m app.ops.wslconf merge /etc/wsl.conf)
        if [ -f /etc/wsl.conf ]; then
            run cp -p /etc/wsl.conf "/etc/wsl.conf.bak-broke-ai-coder-$(date +%Y%m%d%H%M%S)"
        fi
        WSLCONF_TMP=$(mktemp -p /root broke-ai-coder.wslconf.XXXXXX)
        printf '%s\n' "$merged" >"$WSLCONF_TMP"
        run install -m 0644 -o root -g root "$WSLCONF_TMP" /etc/wsl.conf
        changed "updated /etc/wsl.conf (backup kept next to it)"
        NEED_SHUTDOWN=1
        ;;
    *) fail "could not read /etc/wsl.conf" ;;
esac
for f in /proc/sys/fs/binfmt_misc/WSLInterop*; do
    if [ -e "$f" ] && [ "$(head -n 1 "$f")" = "enabled" ]; then
        note "WSL interop is enabled right now; wsl --shutdown is required"
        NEED_SHUTDOWN=1
    fi
done

# 9. sudoers
SUDOERS_DEST=/etc/sudoers.d/broke-ai-coder
run visudo -cf "$SUDOERS_TMP"
if [ -f "$SUDOERS_DEST" ] && cmp -s "$SUDOERS_TMP" "$SUDOERS_DEST"; then
    ok "$SUDOERS_DEST is up to date"
else
    BACKUP=""
    if [ -f "$SUDOERS_DEST" ]; then
        BACKUP="/root/broke-ai-coder.sudoers.bak-$(date +%Y%m%d%H%M%S)"
        run cp -p "$SUDOERS_DEST" "$BACKUP"
    fi
    run install -m 0440 -o root -g root "$SUDOERS_TMP" "$SUDOERS_DEST"
    if ! run visudo -c; then
        if [ -n "$BACKUP" ]; then
            run install -m 0440 -o root -g root "$BACKUP" "$SUDOERS_DEST"
        else
            run rm -f "$SUDOERS_DEST"
        fi
        printf '[setup] FAIL: visudo -c rejected the sudoers file; rolled back\n' >&2
        exit 1
    fi
    changed "installed $SUDOERS_DEST"
fi

# 10. git identity of the OpenCode user (docs C6 step 5)
OC_GITCONFIG="/home/$OC_USER/.gitconfig"
oc_git() {
    printf '[setup] RUN: git config --global %s (as %s)\n' "$*" "$OC_USER"
    setsid -w runuser -u "$OC_USER" -- env HOME="/home/$OC_USER" git config --global "$@" </dev/null 2>&1 | cat
}
if [ -e "$OC_GITCONFIG" ]; then
    ok "$OC_GITCONFIG exists; not touched"
else
    oc_git user.name "broke-ai-coder agent"
    oc_git user.email "agent@example.invalid"
    oc_git --add safe.directory '*'
    changed "set the git identity of $OC_USER"
fi

# ---- Phase C: summary ---------------------------------------------------------------------------

echo
echo "[setup] DONE. Changed:${CHANGED:- (nothing; everything was already in place)}"
echo
echo "NEXT:"
if [ "$NEED_SHUTDOWN" -eq 1 ] || [ "$GROUP_CHANGED" -eq 1 ]; then
    echo "  1. In Windows PowerShell run: wsl --shutdown   then reopen Ubuntu (applies wsl.conf and group changes)"
fi
echo "  2. Fill in the secrets file: nano $SECRETS   (names in secrets.env.example)"
echo "  3. Run scripts/live-checks.sh   (from a shell without secrets)"
echo "  4. Run scripts/start-daemon.sh"

if command -v git >/dev/null 2>&1; then
    git_version=$(git --version | awk '{print $3}')
    if [ "$(printf '%s\n%s\n' 2.45.1 "$git_version" | sort -V | head -n 1)" != "2.45.1" ]; then
        echo
        echo "[setup] git $git_version is older than 2.45.1; to update, run:"
        echo "  sudo add-apt-repository ppa:git-core/ppa && sudo apt update && sudo apt install -y git"
    fi
fi
