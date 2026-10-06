#!/usr/bin/env bash
set -eu
# Run the broke-ai-coder daemon in the foreground (tmux or systemd).
# Secrets are loaded here, in this process only, never from ~/.bashrc.

REPO_DIR=$(cd "$(dirname "$0")/.." && pwd -P)
PY="$REPO_DIR/.venv/bin/python"
CONFIG="$REPO_DIR/config.yaml"
SECRETS="$HOME/.config/broke-ai-coder/secrets.env"

usage() {
    cat <<'USAGE'
Usage: scripts/start-daemon.sh [--config PATH] [--secrets PATH]

Runs the daemon in the foreground (use tmux, or the systemd unit in scripts/systemd/).
Stop it with scripts/stop-daemon.sh (under systemd: sudo systemctl stop broke-ai-coder).

  --config PATH   config file (default: <repo>/config.yaml)
  --secrets PATH  env file with the tokens and keys (default:
                  ~/.config/broke-ai-coder/secrets.env; must be a regular file you own
                  with mode 600 or 400)
  -h, --help      show this help

Exit codes: 0 ok, 2 refused or usage error. Secret values are never printed.
USAGE
}

refuse() {
    printf 'start-daemon: refused: %s\n' "$1" >&2
    exit 2
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
        --secrets)
            [ $# -ge 2 ] || {
                usage >&2
                exit 2
            }
            SECRETS=$2
            shift 2
            ;;
        *)
            printf 'start-daemon: unknown argument: %s\n' "$1" >&2
            usage >&2
            exit 2
            ;;
    esac
done

case "$CONFIG" in
    /*) ;;
    *) CONFIG="$PWD/$CONFIG" ;; # relative to where you ran it, not to the repo
esac
[ "$(id -u)" -ne 0 ] || refuse "do not run the daemon as root"
[ -x "$PY" ] || refuse "$PY is not executable (create the venv, setup guide E1)"
[ -f "$CONFIG" ] || refuse "config file not found: $CONFIG"

[ -e "$SECRETS" ] || [ -L "$SECRETS" ] || refuse "secrets file not found: $SECRETS (see secrets.env.example)"
[ ! -L "$SECRETS" ] || refuse "secrets file is a symlink: $SECRETS"
[ -f "$SECRETS" ] || refuse "secrets file is not a regular file: $SECRETS"
[ "$(stat -c %u "$SECRETS")" = "$(id -u)" ] || refuse "secrets file is not owned by you: $SECRETS"
mode=$(stat -c %a "$SECRETS")
case "$mode" in
    600 | 400) ;;
    *) refuse "secrets file mode is $mode, must be 600 or 400 (chmod 600 $SECRETS)" ;;
esac
# Only line numbers are printed, never the content of a line.
bad_lines=$(grep -nvE '^[[:space:]]*($|#)|^(export )?[A-Za-z_][A-Za-z0-9_]*=' "$SECRETS" | cut -d: -f1 | tr '\n' ' ' || true)
[ -z "$bad_lines" ] || refuse "secrets file has malformed lines (line numbers: $bad_lines)"

STATE_DIR="${XDG_STATE_HOME:-$HOME/.local/state}/broke-ai-coder"
PIDFILE="$STATE_DIR/daemon.pid"
mkdir -p "$STATE_DIR"
chmod 700 "$STATE_DIR"

if [ -f "$PIDFILE" ]; then
    old_pid=$(cat "$PIDFILE" 2>/dev/null || true)
    case "$old_pid" in
        '' | *[!0-9]*) old_pid="" ;;
    esac
    if [ -n "$old_pid" ] && kill -0 "$old_pid" 2>/dev/null &&
        tr '\0' ' ' <"/proc/$old_pid/cmdline" 2>/dev/null | grep -q -- '-m app'; then
        refuse "the daemon is already running (pid $old_pid)"
    fi
    rm -f "$PIDFILE"
fi

cd "$REPO_DIR"
umask 077
echo $$ >"$PIDFILE"
set -a
# shellcheck source=/dev/null
. "$SECRETS"
set +a
exec "$PY" -m app --config "$CONFIG"
