#!/usr/bin/env sh
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
PYTHON=/Users/rstiskalek/Tools/venv_tools/bin/python3

exec "$PYTHON" -B "$SCRIPT_DIR/check_ads_bib.py" "$@"
