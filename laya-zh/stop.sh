#!/bin/sh
set -eu

DEPLOY_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
RUNTIME_DIR="$DEPLOY_DIR/runtime"
PID_FILE="$RUNTIME_DIR/server.pid"
HOST=127.0.0.1
PORT=8765

if [ ! -f "$PID_FILE" ]; then
    echo "Laya service already stopped"
    exit 0
fi

pid=$(sed -n '1p' "$PID_FILE")
case "$pid" in
    ''|*[!0-9]*)
        echo "Invalid PID file; refusing to stop anything: $PID_FILE" >&2
        exit 1
        ;;
esac

command_line=$(ps -p "$pid" -o command= 2>/dev/null || true)
case "$command_line" in
    *"$DEPLOY_DIR/server.py"*"--host $HOST"*"--port $PORT"*) ;;
    *)
        if kill -0 "$pid" 2>/dev/null; then
            echo "PID file does not identify this Laya service; refusing to stop pid=$pid" >&2
            exit 1
        fi
        rm -f "$PID_FILE"
        echo "Laya service already stopped (stale PID removed)"
        exit 0
        ;;
esac

kill "$pid" 2>/dev/null || true
attempt=0
while [ "$attempt" -lt 20 ]; do
    if ! kill -0 "$pid" 2>/dev/null; then
        rm -f "$PID_FILE"
        echo "Laya service stopped"
        exit 0
    fi
    attempt=$((attempt + 1))
    sleep 1
done

echo "Laya service did not stop within 20 seconds; pid=$pid" >&2
exit 1
