#!/bin/sh
set -eu

STATE_DIR="${STORAGE_HYGIENE_STATE_DIR:-$HOME/.local/state/ralfloop-storage-hygiene}"
PROJECT_DIR="${STORAGE_HYGIENE_PROJECT_DIR:-$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)}"
CC_BIN="${STORAGE_HYGIENE_CC:-cc}"
TMP_ROOT="${STORAGE_HYGIENE_TMP_ROOT:-/tmp}"
AGE_DAYS="${STORAGE_HYGIENE_AGE_DAYS:-2}"
MIN_MIB="${STORAGE_HYGIENE_MIN_MIB:-100}"

mkdir -p "$STATE_DIR"
src="$PROJECT_DIR/tools/storage_hygiene_audit.c"
bin="$STATE_DIR/storage_hygiene_audit"
tmp_bin="$bin.tmp.$$"
tmp_json="$STATE_DIR/latest.json.tmp.$$"

if [ ! -x "$bin" ] || [ "$src" -nt "$bin" ]; then
    "$CC_BIN" -std=c11 -O2 -Wall -Wextra -Werror "$src" -o "$tmp_bin"
    mv "$tmp_bin" "$bin"
fi

"$bin" \
    --tmp "$TMP_ROOT" \
    --age-days "$AGE_DAYS" \
    --min-mib "$MIN_MIB" \
    "$@" >"$tmp_json"

mv "$tmp_json" "$STATE_DIR/latest.json"
