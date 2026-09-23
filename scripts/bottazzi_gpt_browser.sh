#!/usr/bin/env bash
set -euo pipefail

CHROME_BIN="${BOTTAZZI_GPT_CHROME_BIN:-/usr/bin/google-chrome}"
PROFILE_DIR="${BOTTAZZI_GPT_PROFILE_DIR:-/home/bandi/.local/share/bottazzi-gpt-browser/profile}"
CACHE_DIR="${BOTTAZZI_GPT_CACHE_DIR:-/tmp/bottazzi-gpt-browser-cache}"
CDP_PORT="${BOTTAZZI_GPT_CDP_PORT:-9237}"
HEADLESS="${BOTTAZZI_GPT_HEADLESS:-1}"

if [[ ! -x "$CHROME_BIN" ]]; then
  echo "Chrome binary not found: $CHROME_BIN" >&2
  exit 2
fi

mkdir -p "$PROFILE_DIR" "$CACHE_DIR"
chmod 700 "$PROFILE_DIR"

args=(
  --remote-debugging-address=127.0.0.1
  "--remote-debugging-port=${CDP_PORT}"
  "--user-data-dir=${PROFILE_DIR}"
  "--disk-cache-dir=${CACHE_DIR}"
  --disk-cache-size=67108864
  --no-first-run
  --no-default-browser-check
  --disable-default-apps
  --disable-sync
  --password-store=basic
)

if [[ "$HEADLESS" == "1" ]]; then
  args+=(--headless=new)
fi

exec "$CHROME_BIN" "${args[@]}" about:blank
