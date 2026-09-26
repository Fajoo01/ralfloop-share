#!/bin/sh
set -u

STATE_DIR="${STORAGE_CHECK_STATE_DIR:-$HOME/.local/state/ralfloop-storage-checker}"
PYTHON_BIN="${STORAGE_CHECK_PYTHON:-/usr/bin/python3}"
PROJECT_DIR="${STORAGE_CHECK_PROJECT_DIR:-$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)}"
mkdir -p "$STATE_DIR"

tmp="$STATE_DIR/latest.json.tmp.$$"
cd "$PROJECT_DIR" || exit 3

"$PYTHON_BIN" -m ralfloop_agent.storage_checker >"$tmp"
rc=$?

mv "$tmp" "$STATE_DIR/latest.json"
printf '%s\n' "$rc" >"$STATE_DIR/last-exit-code"

if grep -q '"type": "disk_purchase_research"' "$STATE_DIR/latest.json"; then
    cp "$STATE_DIR/latest.json" "$STATE_DIR/research-trigger.json"
else
    rm -f "$STATE_DIR/research-trigger.json"
fi

exit "$rc"
