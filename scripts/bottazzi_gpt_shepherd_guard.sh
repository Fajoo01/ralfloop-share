#!/usr/bin/env bash
set -u

ROOT="/home/bandi/ralfloop-bottazzi-gpt-rollover-20260923"
PY="/home/sibilla-cumana/ralfloop_agent_scaffold/.venv/bin/python"
TOOL="$ROOT/tools/bottazzi_gpt_session.py"
ENDPOINT="http://127.0.0.1:9238"
SNOOZE_FILE="/run/user/1001/bottazzi-gpt-rollover-snooze-until"
COUNTDOWN_SECONDS=30
SNOOZE_SECONDS=600

now=$(date +%s)
if [[ -f "$SNOOZE_FILE" ]]; then
  read -r snooze_until < "$SNOOZE_FILE" || snooze_until=0
  if [[ "$snooze_until" =~ ^[0-9]+$ ]] && (( now < snooze_until )); then
    exit 0
  fi
fi

probe="$($PY "$TOOL" --endpoint "$ENDPOINT" shepherd 2>/dev/null || true)"
[[ -n "$probe" ]] || exit 0

read -r rollover ready < <(printf '%s' "$probe" | "$PY" -c 'import json,sys; d=json.load(sys.stdin); ui=d.get("ui") or {}; print(1 if d.get("rollover") else 0, 1 if ui.get("ready") else 0)' 2>/dev/null || echo '0 0')
[[ "$rollover" == "1" && "$ready" == "1" ]] || exit 0

if (
  for ((i=0; i<=COUNTDOWN_SECONDS; i++)); do
    pct=$(( i * 100 / COUNTDOWN_SECONDS ))
    remaining=$(( COUNTDOWN_SECONDS - i ))
    echo "$pct"
    echo "# La chat sta diventando pesante.\nPassaggio automatico a una nuova chat tra ${remaining} secondi.\nPremi 'Rinvia 10 min' per restare qui."
    sleep 1
  done
  echo 100
) | zenity --progress \
    --title="Bot-tazzi GPT" \
    --text="Preparazione rollover..." \
    --percentage=0 \
    --auto-close \
    --time-remaining \
    --cancel-label="Rinvia 10 min" \
    --width=540 \
    --height=150; then
  exec "$PY" "$TOOL" --endpoint "$ENDPOINT" shepherd --apply --submit
else
  printf '%s\n' "$(( $(date +%s) + SNOOZE_SECONDS ))" > "$SNOOZE_FILE"
  exit 0
fi
