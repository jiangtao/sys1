#!/bin/sh
set -eu

HERE=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
KEV_DIR="$HERE"
PYTHON="$KEV_DIR/.venv/bin/python"
RUNTIME="$HERE/runtime"
PID_FILE="$RUNTIME/server.pid"
LOG_FILE="$RUNTIME/server.log"
PORT=8009
MODEL=jaredpalmer/kev-0.8b@54f4f8777356cd5bbbb6c6919c657f26e6f2f6d8

is_kev_pid() {
    command_line=$(ps -p "$1" -o command= 2>/dev/null || true)
    case "$command_line" in
        *"kev.serve --run $MODEL --port $PORT"*) return 0 ;;
        *) return 1 ;;
    esac
}

read_pid() {
    [ -f "$PID_FILE" ] || return 1
    pid=$(sed -n '1p' "$PID_FILE")
    case "$pid" in
        ''|*[!0-9]*) echo "Invalid PID file: $PID_FILE" >&2; exit 1 ;;
    esac
}

case "${1:-}" in
    start)
        [ -x "$PYTHON" ] || { echo "Missing $PYTHON; run uv sync --extra serve --no-dev --python 3.12 in $KEV_DIR" >&2; exit 1; }
        mkdir -p "$RUNTIME"
        if read_pid; then
            if kill -0 "$pid" 2>/dev/null; then
                if is_kev_pid "$pid"; then echo "Kev already running (pid=$pid)"; exit 0; fi
                echo "PID file points to another process; refusing to overwrite: $pid" >&2
                exit 1
            fi
            rm -f "$PID_FILE"
        fi
        pid=$("$PYTHON" - "$PYTHON" "$MODEL" "$PORT" "$LOG_FILE" "$KEV_DIR" "$RUNTIME/hf" <<'PY'
import os
import subprocess
import sys

python, model, port, log_file, cwd, hf_home = sys.argv[1:]
env = dict(os.environ, HF_HOME=hf_home, HF_HUB_DISABLE_XET="1")
with open(log_file, "ab") as log:
    process = subprocess.Popen(
        [python, "-m", "kev.serve", "--run", model, "--port", port],
        cwd=cwd,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=log,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
print(process.pid)
PY
        )
        printf '%s\n' "$pid" >"$PID_FILE"
        echo "Kev starting (pid=$pid, http://127.0.0.1:$PORT); check ./control.sh status"
        ;;
    status)
        if ! read_pid; then echo "stopped"; exit 0; fi
        if ! kill -0 "$pid" 2>/dev/null; then
            rm -f "$PID_FILE"
            echo "stopped (stale PID removed)"
            exit 0
        fi
        is_kev_pid "$pid" || { echo "PID file points to another process: $pid" >&2; exit 1; }
        if curl --fail --silent --max-time 2 "http://127.0.0.1:$PORT/v1/models" >/dev/null; then
            echo "ready pid=$pid http://127.0.0.1:$PORT"
        else
            echo "starting-or-unhealthy pid=$pid; see $LOG_FILE"
            exit 1
        fi
        ;;
    stop)
        if ! read_pid; then echo "Kev already stopped"; exit 0; fi
        if ! kill -0 "$pid" 2>/dev/null; then rm -f "$PID_FILE"; echo "Kev already stopped"; exit 0; fi
        is_kev_pid "$pid" || { echo "PID file points to another process; refusing to stop: $pid" >&2; exit 1; }
        kill "$pid"
        echo "Sent stop signal to Kev (pid=$pid)"
        ;;
    *) echo "Usage: $0 {start|status|stop}" >&2; exit 2 ;;
esac
