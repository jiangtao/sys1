#!/bin/sh
set -eu

DEPLOY_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
RUNTIME_DIR="$DEPLOY_DIR/runtime"
PID_FILE="$RUNTIME_DIR/server.pid"
HOST=127.0.0.1
PORT=8765

if [ ! -f "$PID_FILE" ]; then
    echo "stopped"
    exit 0
fi

pid=$(sed -n '1p' "$PID_FILE")
case "$pid" in
    ''|*[!0-9]*)
        echo "invalid-pid-file: $PID_FILE" >&2
        exit 1
        ;;
esac

command_line=$(ps -p "$pid" -o command= 2>/dev/null || true)
case "$command_line" in
    *"$DEPLOY_DIR/server.py"*"--host $HOST"*"--port $PORT"*) ;;
    *)
        if kill -0 "$pid" 2>/dev/null; then
            echo "pidfile-mismatch: pid=$pid" >&2
            exit 1
        fi
        rm -f "$PID_FILE"
        echo "stopped (stale PID removed)"
        exit 0
        ;;
esac

health=$(curl --silent --show-error "http://$HOST:$PORT/health" 2>/dev/null || true)
if printf '%s' "$health" | grep -q '"status":"ready"'; then
    echo "running pid=$pid http://$HOST:$PORT $health"
    exit 0
fi

echo "starting-or-unhealthy pid=$pid $health"
exit 1
