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

run_json() {
  local label="$1"
  shift
  local err_file output status
  err_file="$(mktemp "/run/user/1001/bottazzi-gpt-${label}.XXXXXX")" || return 70
  output="$("$@" 2>"$err_file")"
  status=$?
  if [[ -s "$err_file" ]]; then
    while IFS= read -r line; do
      printf 'bottazzi-gpt-shepherd[%s]: %s\n' "$label" "$line" >&2
    done < "$err_file"
  fi
  rm -f "$err_file"
  if (( status != 0 )); then
    if [[ -n "$output" ]]; then
      printf 'bottazzi-gpt-shepherd[%s]: controller output on failure: %s\n' "$label" "$output" >&2
    fi
    printf 'bottazzi-gpt-shepherd[%s]: command failed with status %d\n' "$label" "$status" >&2
    return "$status"
  fi
  if [[ -z "$output" ]]; then
    printf 'bottazzi-gpt-shepherd[%s]: empty JSON response\n' "$label" >&2
    return 70
  fi
  printf '%s' "$output"
}

now=$(date +%s)
if [[ -f "$SNOOZE_FILE" ]]; then
  read -r snooze_until < "$SNOOZE_FILE" || snooze_until=0
  if [[ "$snooze_until" =~ ^[0-9]+$ ]] && (( now < snooze_until )); then
    exit 0
  fi
fi

probe="$(run_json probe "$PY" "$TOOL" --endpoint "$ENDPOINT" shepherd)" || exit $?
if ! probe_fields="$(printf '%s' "$probe" | "$PY" -c 'import json,sys; d=json.load(sys.stdin); ui=d.get("ui") or {}; print(1 if d.get("ok") else 0, 1 if d.get("rollover") else 0, 1 if ui.get("ready") else 0, 1 if d.get("defer_latency_rollover") else 0, 1 if ui.get("temporary_access_limited") else 0)')"; then
  printf 'bottazzi-gpt-shepherd[probe]: invalid JSON: %s\n' "$probe" >&2
  exit 70
fi
read -r probe_ok rollover ready defer_latency temporary_access_limited <<< "$probe_fields"
if [[ "$temporary_access_limited" == "1" ]]; then
  printf '%s\n' "$(( now + RATE_LIMIT_SNOOZE_SECONDS ))" > "$SNOOZE_FILE"
  exit 0
fi
if [[ "$probe_ok" != "1" ]]; then
  printf 'bottazzi-gpt-shepherd[probe]: controller returned error: %s\n' "$probe" >&2
  exit 1
fi

adoption="$(run_json adoption "$PY" "$TOOL" --endpoint "$ENDPOINT" adopt-external --apply --scan-interval-seconds 30)" || exit $?
if ! adoption_fields="$(printf '%s' "$adoption" | "$PY" -c 'import json,sys; d=json.load(sys.stdin); print(1 if d.get("ok") else 0, str(d.get("action") or ""), str(d.get("reason") or ""))')"; then
  printf 'bottazzi-gpt-shepherd[adoption]: invalid JSON: %s\n' "$adoption" >&2
  exit 70
fi
read -r adoption_ok adoption_action adoption_reason <<< "$adoption_fields"
if [[ "$adoption_reason" == "temporary_access_limited" ]]; then
  printf '%s\n' "$(( now + RATE_LIMIT_SNOOZE_SECONDS ))" > "$SNOOZE_FILE"
  exit 0
fi
if [[ "$adoption_ok" != "1" ]]; then
  printf 'bottazzi-gpt-shepherd[adoption]: controller returned error: %s\n' "$adoption" >&2
  exit 1
fi
[[ "$adoption_action" != "adopted" && "$adoption_action" != "recovered" ]] || exit 0
[[ "$adoption_reason" != "unsent_composer_text" && "$adoption_reason" != "mutation_locked" ]] || exit 0
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
  apply_result="$(run_json rollover_apply "$PY" "$TOOL" --endpoint "$ENDPOINT" shepherd --apply --submit)" || exit $?
  printf '%s\n' "$apply_result"
  if ! apply_fields="$(printf '%s' "$apply_result" | "$PY" -c 'import json,sys; d=json.load(sys.stdin); print(1 if d.get("ok") else 0, str(d.get("action") or ""), str(d.get("reason") or d.get("blocked") or ""))')"; then
    printf 'bottazzi-gpt-shepherd[rollover_apply]: invalid JSON: %s\n' "$apply_result" >&2
    exit 70
  fi
  read -r apply_ok apply_action apply_reason <<< "$apply_fields"
  if [[ "$apply_reason" == "temporary_access_limited" ]]; then
    printf '%s\n' "$(( $(date +%s) + RATE_LIMIT_SNOOZE_SECONDS ))" > "$SNOOZE_FILE"
    exit 0
  fi
  [[ "$apply_reason" != "mutation_locked" ]] || exit 0
  if [[ "$apply_ok" != "1" ]]; then
    printf 'bottazzi-gpt-shepherd[rollover_apply]: controller returned error: %s\n' "$apply_result" >&2
    exit 1
  fi
  exit 0
else
  printf '%s\n' "$(( $(date +%s) + SNOOZE_SECONDS ))" > "$SNOOZE_FILE"
  exit 0
fi
