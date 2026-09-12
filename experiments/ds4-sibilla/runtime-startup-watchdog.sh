#!/usr/bin/env bash
set -euo pipefail

PORT_DIR="${PORT_DIR:-/home/sibilla-cumana/src/ds4-main-lowvram-port}"
MODEL="${MODEL:-/home/sibilla-cumana/Dati/ralfloop-models/deepseek-v4-flash-pr739/gguf/DeepSeek-V4-Flash-IQ2XXS-w2Q2K-AProjQ8-SExpQ8-OutQ8-chat-v2-imatrix-0731.gguf}"
OWNER="${OWNER:-sibilla-cumana}"
TEST_PORT="${TEST_PORT:-19195}"
PROD_PORT="${PROD_PORT:-19194}"
MAX_DIRTY_GROWTH_KB="${MAX_DIRTY_GROWTH_KB:-262144}"
STARTUP_TIMEOUT="${STARTUP_TIMEOUT:-90}"
STABLE_SECONDS="${STABLE_SECONDS:-10}"
OUT="${OUT:-/home/sibilla-cumana/ds4-sibilla-snapshot-20260912/upstream-porting/startup-watchdog}"
BIN="$PORT_DIR/ds4-server"
LOG="$OUT/server.log"
PIDFILE="$OUT/server.pid"
LOCKFILE="$OUT/ds4-startup-watchdog.lock"

sudo -u "$OWNER" -H mkdir -p "$OUT"
: | sudo -u "$OWNER" -H tee "$LOG" >/dev/null

if [[ ! -x "$BIN" ]]; then
  echo "missing_binary: $BIN" >&2
  exit 1
fi
if [[ ! -f "$MODEL" ]]; then
  echo "missing_model: $MODEL" >&2
  exit 1
fi
if ! ss -ltn | awk '{print $4}' | grep -qE "[:.]${PROD_PORT}$"; then
  echo "production_port_missing: $PROD_PORT" >&2
  exit 1
fi
if ss -ltn | awk '{print $4}' | grep -qE "[:.]${TEST_PORT}$"; then
  echo "test_port_busy: $TEST_PORT" >&2
  exit 1
fi

BASE_DIRTY_KB="$(awk '/^Dirty:/ {print $2}' /proc/meminfo)"
BASE_WARNINGS="$(sudo dmesg 2>/dev/null | grep -c 'mpage_prepare_extent_to_map' || true)"
printf 'baseline_dirty_kb=%s\nbaseline_mpage_warnings=%s\n' "$BASE_DIRTY_KB" "$BASE_WARNINGS" | \
  sudo -u "$OWNER" -H tee "$OUT/baseline.txt" >/dev/null

AGENT_WAS_ACTIVE=0
if systemctl --user is-active --quiet ralfloop-agentcpm.service; then
  AGENT_WAS_ACTIVE=1
  echo 'stopping AgentCPM temporarily for VRAM headroom'
  systemctl --user stop ralfloop-agentcpm.service
fi

owner_pid_alive() {
  local pid="$1"
  sudo -u "$OWNER" -H kill -0 "$pid" 2>/dev/null
}

cleanup() {
  set +e
  if [[ -f "$PIDFILE" ]]; then
    local pid
    pid="$(cat "$PIDFILE" 2>/dev/null)"
    if [[ -n "${pid:-}" ]] && owner_pid_alive "$pid"; then
      sudo -u "$OWNER" -H kill -TERM "$pid" 2>/dev/null
      for _ in $(seq 1 30); do
        owner_pid_alive "$pid" || break
        sleep 0.2
      done
      if owner_pid_alive "$pid"; then
        sudo -u "$OWNER" -H kill -KILL "$pid" 2>/dev/null
      fi
    fi
  fi
  if [[ "$AGENT_WAS_ACTIVE" -eq 1 ]]; then
    systemctl --user start ralfloop-agentcpm.service >/dev/null 2>&1
  fi
}
trap cleanup EXIT INT TERM

FREE_MIB="$(nvidia-smi --query-gpu=memory.free --format=csv,noheader,nounits | head -n1 | tr -d ' ')"
echo "gpu_free_mib_after_agent_stop=$FREE_MIB"
if [[ "$FREE_MIB" -lt 4000 ]]; then
  echo "insufficient_free_vram_after_agent_stop: ${FREE_MIB} MiB" >&2
  exit 2
fi

sudo -u "$OWNER" -H sh -c '
  DS4_LOCK_FILE="$5" \
  DS4_CUDA_LOW_VRAM_STAGE_MB=640 \
  DS4_CUDA_LOW_VRAM_RESERVE_MB=512 \
  "$1" -m "$2" \
    --backend cuda \
    --ssd-streaming \
    --cuda-low-vram-stream \
    --ssd-streaming-cold \
    --ctx 4096 \
    --prefill-chunk 128 \
    --threads 8 \
    --tokens 16 \
    --host 127.0.0.1 \
    --port "$3" \
    > "$4" 2>&1 &
  echo $!
' sh "$BIN" "$MODEL" "$TEST_PORT" "$LOG" "$LOCKFILE" | \
  sudo -u "$OWNER" -H tee "$PIDFILE" >/dev/null

PID="$(cat "$PIDFILE")"
echo "startup_test_pid=$PID"

READY=0
STABLE=0
for sec in $(seq 1 "$STARTUP_TIMEOUT"); do
  if ! owner_pid_alive "$PID"; then
    echo "startup_process_exited_at=${sec}s" >&2
    break
  fi

  DIRTY_KB="$(awk '/^Dirty:/ {print $2}' /proc/meminfo)"
  DELTA_KB=$((DIRTY_KB - BASE_DIRTY_KB))
  if (( DELTA_KB < 0 )); then DELTA_KB=0; fi
  WARNINGS="$(sudo dmesg 2>/dev/null | grep -c 'mpage_prepare_extent_to_map' || true)"

  printf 't=%ss dirty_kb=%s delta_kb=%s mpage_warnings=%s\n' \
    "$sec" "$DIRTY_KB" "$DELTA_KB" "$WARNINGS"

  if (( DELTA_KB > MAX_DIRTY_GROWTH_KB )); then
    echo "ABORT_DIRTY_GROWTH: ${DELTA_KB} KiB > ${MAX_DIRTY_GROWTH_KB} KiB" >&2
    exit 3
  fi
  if (( WARNINGS > BASE_WARNINGS )); then
    echo "ABORT_NEW_EXT4_MPAGE_WARNING: ${BASE_WARNINGS} -> ${WARNINGS}" >&2
    exit 4
  fi
  if ! ss -ltn | awk '{print $4}' | grep -qE "[:.]${PROD_PORT}$"; then
    echo "ABORT_PRODUCTION_PORT_LOST: $PROD_PORT" >&2
    exit 5
  fi

  if ss -ltn | awk '{print $4}' | grep -qE "[:.]${TEST_PORT}$"; then
    READY=1
    STABLE=$((STABLE + 1))
    if (( STABLE >= STABLE_SECONDS )); then
      break
    fi
  else
    STABLE=0
  fi
  sleep 1
done

if [[ "$READY" -ne 1 || "$STABLE" -lt "$STABLE_SECONDS" ]]; then
  echo 'startup_watchdog_failed_before_stable_listen' >&2
  tail -n 160 "$LOG" || true
  exit 6
fi

if ! grep -q 'CUDA low-VRAM SSD stream: skipping file-backed model host registration' "$LOG"; then
  echo 'missing_no_host_register_runtime_marker' >&2
  tail -n 160 "$LOG" || true
  exit 7
fi

FINAL_DIRTY_KB="$(awk '/^Dirty:/ {print $2}' /proc/meminfo)"
FINAL_WARNINGS="$(sudo dmesg 2>/dev/null | grep -c 'mpage_prepare_extent_to_map' || true)"

echo '=== STARTUP LOG MARKERS ==='
grep -E 'low-VRAM|skipping file-backed|SSD streaming|listening on|memory:|context buffers' "$LOG" | tail -n 120 || true

echo '=== WATCHDOG RESULT ==='
echo "baseline_dirty_kb=$BASE_DIRTY_KB"
echo "final_dirty_kb=$FINAL_DIRTY_KB"
echo "baseline_mpage_warnings=$BASE_WARNINGS"
echo "final_mpage_warnings=$FINAL_WARNINGS"
echo "stable_listen_seconds=$STABLE"
echo 'STARTUP_WATCHDOG_OK: no inference sent; production untouched; AgentCPM restored on cleanup'
