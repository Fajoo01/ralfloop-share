#!/usr/bin/env bash
set -euo pipefail

ROOT="${BOTTAZZI_GPT_REPO:-/home/bandi/ralfloop-bottazzi-gpt-rollover-20260923}"
PYTHON="${BOTTAZZI_GPT_PYTHON:-/home/sibilla-cumana/ralfloop_agent_scaffold/.venv/bin/python}"
LAUNCHER="${BOTTAZZI_GPT_LAUNCHER:-/home/bandi/.local/share/bottazzi-gpt-browser/bottazzi_gpt_browser.sh}"
SERVICE="bottazzi-gpt-browser.service"

if [[ -z "${DISPLAY:-}" && -z "${WAYLAND_DISPLAY:-}" ]]; then
  echo "Run this helper from a graphical desktop session on Sibilla." >&2
  exit 3
fi

sudo -n systemctl stop "$SERVICE"
restart_service() {
  sudo -n systemctl start "$SERVICE" || true
}
trap restart_service EXIT INT TERM

BOTTAZZI_GPT_HEADLESS=0 "$LAUNCHER" &
chrome_pid=$!

for _ in $(seq 1 30); do
  if "$PYTHON" "$ROOT/tools/bottazzi_gpt_session.py" status >/dev/null 2>&1; then
    break
  fi
  sleep 0.25
done

"$PYTHON" "$ROOT/tools/bottazzi_gpt_session.py" rotate --apply

echo "Complete the ChatGPT login in the dedicated Chrome window, then close that window."
wait "$chrome_pid"
