#!/bin/sh
set -eu

DEPLOY_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
if [ -z "${PYTHON:-}" ]; then
    if [ -x "$DEPLOY_DIR/../laya-zh/.venv/bin/python" ]; then
        PYTHON="$DEPLOY_DIR/../laya-zh/.venv/bin/python"
    else
        PYTHON=python3
    fi
fi
RUNTIME_DIR="$DEPLOY_DIR/runtime"
PID_FILE="$RUNTIME_DIR/server.pid"
LOG_FILE="$RUNTIME_DIR/server.log"
HOST=127.0.0.1
PORT=8768

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
            echo "Xiangqi service already running (pid=$pid, http://$HOST:$PORT)"
            exit 0
        fi
        echo "PID file points to another process; refusing to overwrite: pid=$pid" >&2
        exit 1
    fi
    rm -f "$PID_FILE"
fi

pid=$("$PYTHON" - "$DEPLOY_DIR" "$HOST" "$PORT" "$LOG_FILE" <<'PY'
import subprocess
import sys
from pathlib import Path

directory, host, port, log_file = sys.argv[1:]
with open(log_file, "ab") as log:
    process = subprocess.Popen(
        [sys.executable, str(Path(directory) / "server.py"), "--host", host, "--port", port],
        cwd=directory,
        stdin=subprocess.DEVNULL,
        stdout=log,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
print(process.pid)
PY
)
printf '%s\n' "$pid" >"$PID_FILE"

attempt=0
while [ "$attempt" -lt 60 ]; do
    if ! kill -0 "$pid" 2>/dev/null; then
        echo "Xiangqi service exited during startup; see $LOG_FILE" >&2
        rm -f "$PID_FILE"
        exit 1
    fi
    if curl --fail --silent --show-error "http://$HOST:$PORT/api/health" >/dev/null 2>&1; then
        echo "Xiangqi service started (pid=$pid, http://$HOST:$PORT)"
        exit 0
    fi
    attempt=$((attempt + 1))
    sleep 1
done

echo "Xiangqi service is still starting; inspect $LOG_FILE" >&2
exit 1
