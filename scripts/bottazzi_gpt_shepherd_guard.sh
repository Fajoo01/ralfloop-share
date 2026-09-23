#!/usr/bin/env bash
set -u

ROOT="/home/bandi/.local/share/bottazzi-gpt-browser/runtime-current"
PY="/home/sibilla-cumana/ralfloop_agent_scaffold/.venv/bin/python"
TOOL="$ROOT/tools/bottazzi_gpt_session.py"
ENDPOINT="http://127.0.0.1:9238"
SNOOZE_FILE="/run/user/1001/bottazzi-gpt-rollover-snooze-until"
COUNTDOWN_SECONDS=30
SNOOZE_SECONDS=600
RATE_LIMIT_SNOOZE_SECONDS=300

now=$(date +%s)
if [[ -f "$SNOOZE_FILE" ]]; then
  read -r snooze_until < "$SNOOZE_FILE" || snooze_until=0
  if [[ "$snooze_until" =~ ^[0-9]+$ ]] && (( now < snooze_until )); then
    exit 0
  fi
fi

probe="$($PY "$TOOL" --endpoint "$ENDPOINT" shepherd 2>/dev/null || true)"
[[ -n "$probe" ]] || exit 0

read -r rollover ready defer_latency temporary_access_limited < <(printf '%s' "$probe" | "$PY" -c 'import json,sys; d=json.load(sys.stdin); ui=d.get("ui") or {}; print(1 if d.get("rollover") else 0, 1 if ui.get("ready") else 0, 1 if d.get("defer_latency_rollover") else 0, 1 if ui.get("temporary_access_limited") else 0)' 2>/dev/null || echo '0 0 0 0')
if [[ "$temporary_access_limited" == "1" ]]; then
  printf '%s\n' "$(( now + RATE_LIMIT_SNOOZE_SECONDS ))" > "$SNOOZE_FILE"
  exit 0
fi

adoption="$($PY "$TOOL" --endpoint "$ENDPOINT" adopt-external --apply --scan-interval-seconds 30 2>/dev/null || true)"
if [[ -n "$adoption" ]]; then
  read -r adoption_action adoption_reason < <(printf '%s' "$adoption" | "$PY" -c 'import json,sys; d=json.load(sys.stdin); print(str(d.get("action") or ""), str(d.get("reason") or ""))' 2>/dev/null || echo 'noop parse_failed')
  if [[ "$adoption_reason" == "temporary_access_limited" ]]; then
    printf '%s\n' "$(( now + RATE_LIMIT_SNOOZE_SECONDS ))" > "$SNOOZE_FILE"
    exit 0
  fi
  [[ "$adoption_action" != "adopted" ]] || exit 0
  [[ "$adoption_reason" != "unsent_composer_text" ]] || exit 0
fi
[[ "$rollover" == "1" && "$ready" == "1" ]] || exit 0
# A handoff's first response may legitimately take time, and an actively
# streaming response should not be killed merely because total latency crossed
# the threshold. Other rollover reasons (turns/age/errors) remain unaffected.
[[ "$defer_latency" == "0" ]] || exit 0

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
