#!/bin/sh
set -eu
GAME_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
exec "$GAME_DIR/../laya-zh/.venv/bin/python" "$GAME_DIR/run.py"
