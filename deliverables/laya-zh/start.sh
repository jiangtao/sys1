#!/bin/sh
set -eu

DEPLOY_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
PYTHON="$DEPLOY_DIR/.venv/bin/python"
RUNTIME_DIR="$DEPLOY_DIR/runtime"
PID_FILE="$RUNTIME_DIR/server.pid"
LOG_FILE="$RUNTIME_DIR/server.log"
HOST=127.0.0.1
PORT=8765

if [ ! -x "$PYTHON" ]; then
    echo "Missing $PYTHON. Create the environment first; see README.md." >&2
    exit 1
fi

mkdir -p "$RUNTIME_DIR"

is_service_pid() {
    service_pid=$1
    command_line=$(ps -p "$service_pid" -o command= 2>/dev/null || true)
    case "$command_line" in
        *"$DEPLOY_DIR/server.py"*"--host $HOST"*"--port $PORT"*) return 0 ;;
        *) return 1 ;;
    esac
}

if [ -f "$PID_FILE" ]; then
    pid=$(sed -n '1p' "$PID_FILE")
    case "$pid" in
        ''|*[!0-9]*)
            echo "Invalid PID file: $PID_FILE" >&2
            exit 1
            ;;
    esac
    if kill -0 "$pid" 2>/dev/null; then
        if is_service_pid "$pid"; then
            echo "Laya service already running (pid=$pid, http://$HOST:$PORT)"
            exit 0
        fi
        echo "PID file points to another process; refusing to overwrite: pid=$pid" >&2
        exit 1
    fi
    rm -f "$PID_FILE"
fi

(
    cd "$DEPLOY_DIR"
    exec nohup "$PYTHON" "$DEPLOY_DIR/server.py" --host "$HOST" --port "$PORT"
) >>"$LOG_FILE" 2>&1 </dev/null &
pid=$!
printf '%s\n' "$pid" >"$PID_FILE"

attempt=0
while [ "$attempt" -lt 60 ]; do
    if ! kill -0 "$pid" 2>/dev/null; then
        echo "Laya service exited during startup; see $LOG_FILE" >&2
        rm -f "$PID_FILE"
        exit 1
    fi
    if curl --fail --silent --show-error "http://$HOST:$PORT/health" 2>/dev/null | grep -q '"status":"ready"'; then
        echo "Laya service started (pid=$pid, http://$HOST:$PORT)"
        exit 0
    fi
    attempt=$((attempt + 1))
    sleep 1
done

echo "Laya service is still starting; inspect $LOG_FILE and run ./status.sh" >&2
exit 1
