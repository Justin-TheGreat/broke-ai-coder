#!/usr/bin/env bash
set -eu
# Stop the daemon started by scripts/start-daemon.sh.

REPO_DIR=$(cd "$(dirname "$0")/.." && pwd -P)
PY="$REPO_DIR/.venv/bin/python"
CONFIG="$REPO_DIR/config.yaml"
TIMEOUT=30
FORCE=0

usage() {
    cat <<'USAGE'
Usage: scripts/stop-daemon.sh [--timeout N] [--force] [--config PATH]

Sends SIGTERM to the daemon recorded in the PID file and waits for it to exit.

  --timeout N    seconds to wait for a clean exit (default 30)
  --force        SIGKILL the daemon if it is still alive after the timeout
  --config PATH  accepted for symmetry with the other scripts (unused)
  -h, --help     show this help

Under systemd use: sudo systemctl stop broke-ai-coder
Exit codes: 0 stopped or not running, 1 still running after the timeout, 2 refused or usage error.
USAGE
}

while [ $# -gt 0 ]; do
    case "$1" in
        -h | --help)
            usage
            exit 0
            ;;
        --timeout)
            [ $# -ge 2 ] || {
                usage >&2
                exit 2
            }
            TIMEOUT=$2
            shift 2
            ;;
        --force)
            FORCE=1
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
            printf 'stop-daemon: unknown argument: %s\n' "$1" >&2
            usage >&2
            exit 2
            ;;
    esac
done
: "$PY" "$CONFIG"
case "$TIMEOUT" in
    '' | *[!0-9]*)
        printf 'stop-daemon: --timeout must be a whole number of seconds\n' >&2
        exit 2
        ;;
esac

PIDFILE="${XDG_STATE_HOME:-$HOME/.local/state}/broke-ai-coder/daemon.pid"
if [ ! -f "$PIDFILE" ]; then
    echo "not running"
    exit 0
fi
pid=$(cat "$PIDFILE" 2>/dev/null || true)
case "$pid" in
    '' | *[!0-9]*)
        echo "not running (removing malformed PID file)"
        rm -f "$PIDFILE"
        exit 0
        ;;
esac
if ! kill -0 "$pid" 2>/dev/null; then
    echo "not running (removing stale PID file)"
    rm -f "$PIDFILE"
    exit 0
fi
if ! tr '\0' ' ' <"/proc/$pid/cmdline" 2>/dev/null | grep -q -- '-m app'; then
    printf 'stop-daemon: refused: pid %s is not the daemon (command line has no "-m app")\n' "$pid" >&2
    exit 2
fi
owner=$(ps -o user= -p "$pid" | tr -d ' ')
if [ "$owner" != "$(id -un)" ]; then
    printf 'stop-daemon: refused: pid %s is owned by another user\n' "$pid" >&2
    exit 2
fi

echo "stopping daemon (pid $pid), waiting up to ${TIMEOUT}s"
kill -TERM "$pid"
waited=0
while kill -0 "$pid" 2>/dev/null && [ "$waited" -lt "$TIMEOUT" ]; do
    sleep 1
    waited=$((waited + 1))
done
if ! kill -0 "$pid" 2>/dev/null; then
    rm -f "$PIDFILE"
    echo "stopped"
    exit 0
fi
if [ "$FORCE" -eq 1 ]; then
    kill -KILL "$pid" 2>/dev/null || true
    rm -f "$PIDFILE"
    echo "killed (SIGKILL)"
    echo "OpenCode user processes are swept at the next daemon start"
    exit 0
fi
printf 'stop-daemon: still running after %ss; retry with --force\n' "$TIMEOUT" >&2
exit 1
