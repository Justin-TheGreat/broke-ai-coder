#!/usr/bin/env bash
set -euo pipefail
# Offline health checks: config + database, OpenCode user isolation, daemon state. No secrets.

REPO_DIR=$(cd "$(dirname "$0")/.." && pwd -P)
PY="$REPO_DIR/.venv/bin/python"
CONFIG="$REPO_DIR/config.yaml"

usage() {
    cat <<'USAGE'
Usage: scripts/health-check.sh [--config PATH]

Runs `python -m app --check` and `--check-os-user`, and reports whether the daemon runs.
Loads no secrets. Exit codes: 0 all checks passed, 1 a check failed, 2 usage error.
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
            printf 'health-check: unknown argument: %s\n' "$1" >&2
            usage >&2
            exit 2
            ;;
    esac
done

cd "$REPO_DIR"
failed=0

run_step() {
    label=$1
    shift
    set +e
    "$@"
    rc=$?
    set -e
    if [ "$rc" -eq 0 ]; then
        printf 'PASS %s\n' "$label"
    else
        printf 'FAIL %s (exit %s)\n' "$label" "$rc"
        failed=1
    fi
}

run_step "config and database (python -m app --check)" "$PY" -m app --check --config "$CONFIG"
run_step "OpenCode user isolation (python -m app --check-os-user)" "$PY" -m app --check-os-user --config "$CONFIG"

PIDFILE="${XDG_STATE_HOME:-$HOME/.local/state}/broke-ai-coder/daemon.pid"
state="not running"
if [ -f "$PIDFILE" ]; then
    pid=$(cat "$PIDFILE" 2>/dev/null || true)
    case "$pid" in
        '' | *[!0-9]*) ;;
        *)
            if kill -0 "$pid" 2>/dev/null &&
                tr '\0' ' ' <"/proc/$pid/cmdline" 2>/dev/null | grep -q -- '-m app'; then
                state="running (pid $pid)"
            fi
            ;;
    esac
fi
printf 'INFO daemon: %s\n' "$state"

exit "$failed"
