#!/usr/bin/env sh
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
PYTHON_BIN=${CHECK_ADS_BIB_PYTHON:-${PYTHON:-python3}}

exec "$PYTHON_BIN" -B "$SCRIPT_DIR/check_ads_bib.py" "$@"
