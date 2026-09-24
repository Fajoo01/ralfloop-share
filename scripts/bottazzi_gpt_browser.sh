#!/usr/bin/env bash
set -euo pipefail

CHROME_BIN="${BOTTAZZI_GPT_CHROME_BIN:-/usr/bin/google-chrome}"
PROFILE_DIR="${BOTTAZZI_GPT_PROFILE_DIR:-/home/bandi/.local/share/bottazzi-gpt-browser/profile}"
CACHE_DIR="${BOTTAZZI_GPT_CACHE_DIR:-/run/bottazzi-gpt-browser-cache}"
CONFIG_DIR="${BOTTAZZI_GPT_CONFIG_DIR:-/home/bandi/.local/share/bottazzi-gpt-browser/config}"
CDP_PORT="${BOTTAZZI_GPT_CDP_PORT:-9238}"
HEADLESS="${BOTTAZZI_GPT_HEADLESS:-1}"
START_URL="${BOTTAZZI_GPT_START_URL:-https://chatgpt.com/}"
WORKSPACE="${BOTTAZZI_GPT_WORKSPACE:-3}"

if [[ ! -x "$CHROME_BIN" ]]; then
  echo "Chrome binary not found: $CHROME_BIN" >&2
  exit 2
fi

mkdir -p "$PROFILE_DIR" "$CACHE_DIR" "$CONFIG_DIR"
chmod 700 "$PROFILE_DIR" "$CACHE_DIR" "$CONFIG_DIR"

args=(
  --remote-debugging-address=127.0.0.1
  "--remote-debugging-port=${CDP_PORT}"
  "--user-data-dir=${PROFILE_DIR}"
  "--disk-cache-dir=${CACHE_DIR}"
  --disk-cache-size=67108864
  --no-first-run
  --no-default-browser-check
  --restore-last-session
  --disable-default-apps
  --disable-sync
  --disable-breakpad
  --disable-crash-reporter
  --password-store=basic
)

if [[ "$HEADLESS" == "1" ]]; then
  args+=(--headless=new)
else
  browser_pid="$$"
  if command -v xdotool >/dev/null 2>&1; then
    (
      for _ in $(seq 1 80); do
        moved=0
        while read -r window_id; do
          [[ -n "$window_id" ]] || continue
          if xdotool set_desktop_for_window "$window_id" "$WORKSPACE" >/dev/null 2>&1; then
            moved=1
          fi
        done < <(xdotool search --pid "$browser_pid" --class chrome 2>/dev/null || true)
        [[ "$moved" == "1" ]] && exit 0
        sleep 0.25
      done
    ) &
  fi
fi

exec "$CHROME_BIN" "${args[@]}" "$START_URL"
