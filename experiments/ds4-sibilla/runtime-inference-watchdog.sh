#!/usr/bin/env bash
set -euo pipefail

PORT_DIR="${PORT_DIR:-/home/sibilla-cumana/src/ds4-main-lowvram-port}"
OWNER="${OWNER:-sibilla-cumana}"
MODEL="${MODEL:-/home/sibilla-cumana/Dati/ralfloop-models/deepseek-v4-flash-pr739/gguf/DeepSeek-V4-Flash-IQ2XXS-w2Q2K-AProjQ8-SExpQ8-OutQ8-chat-v2-imatrix-0731.gguf}"
TEST_PORT="${TEST_PORT:-19195}"
PROD_PORT="${PROD_PORT:-19194}"
OUT="${OUT:-/home/sibilla-cumana/ds4-sibilla-snapshot-20260912/upstream-porting/inference-watchdog}"
DIRTY_ABORT_KB="${DIRTY_ABORT_KB:-131072}"
WRITEBACK_ABORT_KB="${WRITEBACK_ABORT_KB:-65536}"
WATCH_INTERVAL="${WATCH_INTERVAL:-0.25}"
POST_KILL_SECONDS="${POST_KILL_SECONDS:-12}"
REQ_TIMEOUT="${REQ_TIMEOUT:-90}"

BIN="$PORT_DIR/ds4-server"
LOG="$OUT/server.log"
REQ="$OUT/request.json"
RESP="$OUT/response.json"
CURLERR="$OUT/curl.stderr"
PIDFILE="$OUT/server.pid"
LOCKFILE="$OUT/ds4-inference-watchdog.lock"

sudo -u "$OWNER" -H mkdir -p "$OUT"
: | sudo -u "$OWNER" -H tee "$LOG" >/dev/null
: | sudo -u "$OWNER" -H tee "$RESP" >/dev/null
: | sudo -u "$OWNER" -H tee "$CURLERR" >/dev/null

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

mem_kb() {
  awk -v key="$1:" '$1 == key {print $2; exit}' /proc/meminfo
}
mpage_count() {
  sudo dmesg 2>/dev/null | grep -c 'mpage_prepare_extent_to_map' || true
}
owner_pid_alive() {
  local pid="$1"
  sudo -u "$OWNER" -H kill -0 "$pid" 2>/dev/null
}

AGENT_WAS_ACTIVE=0
if systemctl --user is-active --quiet ralfloop-agentcpm.service; then
  AGENT_WAS_ACTIVE=1
  echo "stopping AgentCPM temporarily for VRAM headroom"
  systemctl --user stop ralfloop-agentcpm.service
fi

TEST_PID=""
CURL_PID=""
cleanup() {
  if [[ -n "${CURL_PID:-}" ]] && kill -0 "$CURL_PID" 2>/dev/null; then
    kill -TERM "$CURL_PID" 2>/dev/null || true
  fi
  if [[ -n "${TEST_PID:-}" ]] && owner_pid_alive "$TEST_PID"; then
    sudo -u "$OWNER" -H kill -TERM "$TEST_PID" 2>/dev/null || true
    for _ in $(seq 1 30); do
      owner_pid_alive "$TEST_PID" || break
      sleep 0.2
    done
    if owner_pid_alive "$TEST_PID"; then
      sudo -u "$OWNER" -H kill -KILL "$TEST_PID" 2>/dev/null || true
    fi
  fi
  if [[ "$AGENT_WAS_ACTIVE" -eq 1 ]]; then
    systemctl --user start ralfloop-agentcpm.service || true
  fi
}
trap cleanup EXIT INT TERM

BASE_MPAGE="$(mpage_count)"
BASE_MODEL_STAT="$(stat -c 'inode=%i size=%s mtime=%Y ctime=%Z' "$MODEL")"
BASE_DIRTY="$(mem_kb Dirty)"
BASE_WRITEBACK="$(mem_kb Writeback)"
printf 'baseline_dirty_kb=%s\nbaseline_writeback_kb=%s\nbaseline_mpage=%s\nmodel=%s\n' \
  "$BASE_DIRTY" "$BASE_WRITEBACK" "$BASE_MPAGE" "$BASE_MODEL_STAT"

FREE_MIB="$(nvidia-smi --query-gpu=memory.free --format=csv,noheader,nounits | head -n1 | tr -d ' ')"
echo "gpu_free_mib_after_agent_stop=$FREE_MIB"
if [[ "$FREE_MIB" -lt 4500 ]]; then
  echo "insufficient_free_vram: $FREE_MIB MiB" >&2
  exit 2
fi

sudo -u "$OWNER" -H sh -c '
  DS4_LOCK_FILE="$5" \
  DS4_CUDA_LOW_VRAM_STAGE_MB=640 \
  DS4_CUDA_LOW_VRAM_RESERVE_MB=512 \
  "$1" \
    -m "$2" \
    --backend cuda \
    --ssd-streaming \
    --cuda-low-vram-stream \
    --ssd-streaming-cold \
    --ctx 4096 \
    --prefill-chunk 128 \
    --threads 8 \
    --tokens 8 \
    --host 127.0.0.1 \
    --port "$3" \
    > "$4" 2>&1 &
  echo $!
' sh "$BIN" "$MODEL" "$TEST_PORT" "$LOG" "$LOCKFILE" | sudo -u "$OWNER" -H tee "$PIDFILE" >/dev/null

TEST_PID="$(cat "$PIDFILE")"
echo "startup_test_pid=$TEST_PID"

READY=0
for _ in $(seq 1 240); do
  if ss -ltn | awk '{print $4}' | grep -qE "[:.]${TEST_PORT}$"; then
    READY=1
    break
  fi
  if ! owner_pid_alive "$TEST_PID"; then
    break
  fi
  sleep 0.5
done
if [[ "$READY" -ne 1 ]]; then
  echo "startup_failed_before_listen" >&2
  tail -n 120 "$LOG" || true
  exit 3
fi

if ! grep -q 'CUDA low-VRAM SSD stream: skipping file-backed model host registration' "$LOG"; then
  echo "required_host_registration_skip_marker_missing" >&2
  tail -n 120 "$LOG" || true
  exit 4
fi

PRE_DIRTY="$(mem_kb Dirty)"
PRE_WRITEBACK="$(mem_kb Writeback)"
PRE_MPAGE="$(mpage_count)"
echo "pre_inference_dirty_kb=$PRE_DIRTY"
echo "pre_inference_writeback_kb=$PRE_WRITEBACK"
echo "pre_inference_mpage=$PRE_MPAGE"

printf '%s\n' '{"model":"deepseek-v4-flash","messages":[{"role":"user","content":"Reply with one word: OK"}],"stream":false,"think":false,"max_tokens":1,"temperature":0}' | \
  sudo -u "$OWNER" -H tee "$REQ" >/dev/null

set +e
sudo -u "$OWNER" -H curl --silent --show-error --fail-with-body \
  --connect-timeout 5 --max-time "$REQ_TIMEOUT" \
  -H 'Content-Type: application/json' \
  --data-binary @"$REQ" \
  "http://127.0.0.1:${TEST_PORT}/v1/chat/completions" \
  > "$RESP" 2> "$CURLERR" &
CURL_PID=$!
set -e

ABORT_REASON=""
SAMPLE=0
while kill -0 "$CURL_PID" 2>/dev/null; do
  SAMPLE=$((SAMPLE + 1))
  D="$(mem_kb Dirty)"
  W="$(mem_kb Writeback)"
  M="$(mpage_count)"
  DELTA=$(( D > PRE_DIRTY ? D - PRE_DIRTY : 0 ))
  printf 'sample=%s dirty_kb=%s delta_kb=%s writeback_kb=%s mpage=%s\n' \
    "$SAMPLE" "$D" "$DELTA" "$W" "$M"
  if (( M > PRE_MPAGE )); then
    ABORT_REASON="new_ext4_mpage_warning"
    break
  fi
  if (( DELTA > DIRTY_ABORT_KB )); then
    ABORT_REASON="dirty_growth_over_${DIRTY_ABORT_KB}kb"
    break
  fi
  if (( W > WRITEBACK_ABORT_KB )); then
    ABORT_REASON="writeback_over_${WRITEBACK_ABORT_KB}kb"
    break
  fi
  if ! ss -ltn | awk '{print $4}' | grep -qE "[:.]${PROD_PORT}$"; then
    ABORT_REASON="production_port_lost"
    break
  fi
  sleep "$WATCH_INTERVAL"
done

if [[ -n "$ABORT_REASON" ]]; then
  echo "ABORT_INFERENCE_WATCHDOG: $ABORT_REASON" >&2
  kill -TERM "$CURL_PID" 2>/dev/null || true
  sudo -u "$OWNER" -H kill -TERM "$TEST_PID" 2>/dev/null || true
  sleep 1
  echo '=== LOG TAIL ==='
  tail -n 160 "$LOG" || true
  echo '=== MEMINFO ==='
  grep -E '^(MemAvailable|Dirty|Writeback|SwapTotal|SwapFree):' /proc/meminfo
  exit 5
fi

set +e
wait "$CURL_PID"
CURL_RC=$?
set -e
CURL_PID=""
echo "curl_rc=$CURL_RC"
cat "$RESP" 2>/dev/null || true
cat "$CURLERR" 2>/dev/null || true

# Exercise cleanup/unpin paths while watchdog checks continue afterwards.
if owner_pid_alive "$TEST_PID"; then
  sudo -u "$OWNER" -H kill -TERM "$TEST_PID" 2>/dev/null || true
  for _ in $(seq 1 30); do
    owner_pid_alive "$TEST_PID" || break
    sleep 0.2
  done
  if owner_pid_alive "$TEST_PID"; then
    sudo -u "$OWNER" -H kill -KILL "$TEST_PID" 2>/dev/null || true
  fi
fi
TEST_PID=""

POST_BASE_DIRTY="$(mem_kb Dirty)"
POST_BASE_MPAGE="$(mpage_count)"
echo "post_stop_baseline_dirty_kb=$POST_BASE_DIRTY"
for SEC in $(seq 1 "$POST_KILL_SECONDS"); do
  D="$(mem_kb Dirty)"
  W="$(mem_kb Writeback)"
  M="$(mpage_count)"
  DELTA=$(( D > POST_BASE_DIRTY ? D - POST_BASE_DIRTY : 0 ))
  printf 'post_stop_t=%ss dirty_kb=%s delta_kb=%s writeback_kb=%s mpage=%s\n' \
    "$SEC" "$D" "$DELTA" "$W" "$M"
  if (( M > POST_BASE_MPAGE )); then
    echo "ABORT_POST_STOP_WATCHDOG: new_ext4_mpage_warning" >&2
    exit 6
  fi
  if (( DELTA > DIRTY_ABORT_KB )); then
    echo "ABORT_POST_STOP_WATCHDOG: dirty_growth" >&2
    exit 6
  fi
  sleep 1
done

FINAL_MODEL_STAT="$(stat -c 'inode=%i size=%s mtime=%Y ctime=%Z' "$MODEL")"
FINAL_DIRTY="$(mem_kb Dirty)"
FINAL_WRITEBACK="$(mem_kb Writeback)"
FINAL_MPAGE="$(mpage_count)"

echo '=== FINAL ==='
echo "model_before=$BASE_MODEL_STAT"
echo "model_after =$FINAL_MODEL_STAT"
echo "final_dirty_kb=$FINAL_DIRTY"
echo "final_writeback_kb=$FINAL_WRITEBACK"
echo "final_mpage=$FINAL_MPAGE"
echo '=== LOW-VRAM / INFERENCE LOG ==='
grep -E 'low-VRAM|persistent cache|stage|SSD streaming|skipping file-backed|chat ctx=|decoding|t/s|memory:|CUDA' "$LOG" | tail -n 180 || true

if [[ "$CURL_RC" -ne 0 ]]; then
  echo "INFERENCE_WATCHDOG_REQUEST_FAILED: curl_rc=$CURL_RC" >&2
  exit 7
fi
if [[ "$FINAL_MPAGE" -ne "$BASE_MPAGE" ]]; then
  echo "INFERENCE_WATCHDOG_EXT4_WARNING_DETECTED" >&2
  exit 8
fi
if [[ "$FINAL_MODEL_STAT" != "$BASE_MODEL_STAT" ]]; then
  echo "INFERENCE_WATCHDOG_MODEL_METADATA_CHANGED" >&2
  exit 9
fi

echo 'INFERENCE_WATCHDOG_OK: single-token inference completed; no EXT4 mpage warning; model metadata unchanged; production untouched; AgentCPM restored on cleanup'
