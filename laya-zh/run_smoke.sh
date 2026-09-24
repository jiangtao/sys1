#!/bin/sh
set -eu

DEPLOY_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
PYTHON="$DEPLOY_DIR/.venv/bin/python"

if [ ! -x "$PYTHON" ]; then
    echo "Missing $PYTHON. Create the environment first; see README.md." >&2
    exit 1
fi

exec "$PYTHON" "$DEPLOY_DIR/smoke.py"
