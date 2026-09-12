#!/usr/bin/env bash
set -euo pipefail

PORT_DIR="${PORT_DIR:-/home/sibilla-cumana/src/ds4-main-lowvram-port}"
SNAPSHOT="${SNAPSHOT:-/home/sibilla-cumana/ds4-sibilla-snapshot-20260912}"
OUT="${OUT:-$SNAPSHOT/upstream-porting/runtime-smoke-main-lowvram}"
OWNER="${OWNER:-sibilla-cumana}"
MODEL="${MODEL:-/home/sibilla-cumana/Dati/ralfloop-models/deepseek-v4-flash-pr739/gguf/DeepSeek-V4-Flash-IQ2XXS-w2Q2K-AProjQ8-SExpQ8-OutQ8-chat-v2-imatrix-0731.gguf}"
TEST_PORT="${TEST_PORT:-19195}"
MIN_FREE_MIB="${MIN_FREE_MIB:-2000}"

BIN="$PORT_DIR/ds4-server"
LOG="$OUT/server.log"
PIDFILE="$OUT/server.pid"
LOCKFILE="$OUT/ds4-smoke.lock"
REQ="$OUT/request.json"
RESP="$OUT/response.json"
CURLERR="$OUT/curl.stderr"

sudo -u "$OWNER" -H mkdir -p "$OUT"

if [[ ! -x "$BIN" ]]; then
  echo "missing_binary: $BIN" >&2
  exit 1
fi
if [[ ! -f "$MODEL" ]]; then
  echo "missing_model: $MODEL" >&2
  exit 1
fi
if ss -ltn | awk '{print $4}' | grep -qE "[:.]${TEST_PORT}$"; then
  echo "test_port_busy: $TEST_PORT" >&2
  exit 1
fi

sudo -u "$OWNER" -H sh -c 'git -C "$1" status --short --branch > "$2/source-status.txt"' sh "$PORT_DIR" "$OUT"
sudo -u "$OWNER" -H sh -c 'git -C "$1" diff > "$2/source.patch"' sh "$PORT_DIR" "$OUT"
sudo -u "$OWNER" -H sh -c 'sha256sum "$1/source.patch" > "$1/source.patch.sha256"' sh "$OUT"

FREE_MIB="$(nvidia-smi --query-gpu=memory.free --format=csv,noheader,nounits | head -n1 | tr -d ' ')"
USED_MIB="$(nvidia-smi --query-gpu=memory.used --format=csv,noheader,nounits | head -n1 | tr -d ' ')"
printf 'gpu_used_mib=%s\ngpu_free_mib=%s\nmin_free_mib=%s\nlock_file=%s\n' "$USED_MIB" "$FREE_MIB" "$MIN_FREE_MIB" "$LOCKFILE" | sudo -u "$OWNER" -H tee "$OUT/gpu-before.txt" >/dev/null

echo '=== GPU BEFORE ==='
cat "$OUT/gpu-before.txt"

if [[ "$FREE_MIB" -lt "$MIN_FREE_MIB" ]]; then
  echo "insufficient_free_vram_for_safe_smoke: ${FREE_MIB} MiB < ${MIN_FREE_MIB} MiB" >&2
  echo "No runtime process started."
  exit 2
fi

: | sudo -u "$OWNER" -H tee "$LOG" >/dev/null

owner_pid_alive() {
  local pid="$1"
  sudo -u "$OWNER" -H kill -0 "$pid" 2>/dev/null
}

cleanup() {
  if [[ -f "$PIDFILE" ]]; then
    pid="$(cat "$PIDFILE" 2>/dev/null || true)"
    if [[ -n "${pid:-}" ]] && owner_pid_alive "$pid"; then
      sudo -u "$OWNER" -H kill -TERM "$pid" 2>/dev/null || true
      for _ in $(seq 1 40); do
        owner_pid_alive "$pid" || break
        sleep 0.25
      done
      if owner_pid_alive "$pid"; then
        sudo -u "$OWNER" -H kill -KILL "$pid" 2>/dev/null || true
      fi
    fi
  fi
}
trap cleanup EXIT INT TERM

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
    --tokens 64 \
    --host 127.0.0.1 \
    --port "$3" \
    > "$4" 2>&1 &
  echo $!
' sh "$BIN" "$MODEL" "$TEST_PORT" "$LOG" "$LOCKFILE" | sudo -u "$OWNER" -H tee "$PIDFILE" >/dev/null

PID="$(cat "$PIDFILE")"
echo "runtime_pid=$PID"

READY=0
for i in $(seq 1 240); do
  if ss -ltn | awk '{print $4}' | grep -qE "[:.]${TEST_PORT}$"; then
    READY=1
    break
  fi
  if ! owner_pid_alive "$PID"; then
    break
  fi
  sleep 0.5
done

printf 'ready=%s\npid=%s\nport=%s\nlock_file=%s\n' "$READY" "$PID" "$TEST_PORT" "$LOCKFILE" | sudo -u "$OWNER" -H tee "$OUT/result.txt" >/dev/null

nvidia-smi --query-gpu=memory.used,memory.free --format=csv,noheader,nounits | head -n1 | \
  sudo -u "$OWNER" -H tee "$OUT/gpu-during.txt" >/dev/null

echo
echo '=== RESULT ==='
cat "$OUT/result.txt"
echo
echo '=== GPU DURING STARTUP (used MiB, free MiB) ==='
cat "$OUT/gpu-during.txt"
echo
echo '=== SERVER LOG TAIL ==='
tail -n 160 "$LOG" || true

echo
echo '=== LOW-VRAM STARTUP MARKERS ==='
grep -E 'low-VRAM|persistent cache|cache plan|stage|SSD streaming|listening on|context buffers|memory:' "$LOG" || true

if [[ "$READY" -ne 1 ]]; then
  echo "runtime_smoke_failed_before_listen" >&2
  exit 3
fi

printf '%s\n' '{"model":"deepseek-v4-flash","messages":[{"role":"user","content":"Reply with exactly: DS4_SMOKE_OK"}],"stream":false,"think":false,"max_tokens":8,"temperature":0}' | \
  sudo -u "$OWNER" -H tee "$REQ" >/dev/null

START_TS="$(date +%s)"
if sudo -u "$OWNER" -H sh -c '
  curl --silent --show-error --fail-with-body \
    --connect-timeout 5 --max-time 180 \
    -H "Content-Type: application/json" \
    --data-binary @"$1" \
    "http://127.0.0.1:$2/v1/chat/completions" \
    > "$3" 2> "$4"
' sh "$REQ" "$TEST_PORT" "$RESP" "$CURLERR"; then
  CURL_RC=0
else
  CURL_RC=$?
fi
END_TS="$(date +%s)"
INFERENCE_SECONDS=$((END_TS - START_TS))

nvidia-smi --query-gpu=memory.used,memory.free --format=csv,noheader,nounits | head -n1 | \
  sudo -u "$OWNER" -H tee "$OUT/gpu-after-inference.txt" >/dev/null

INFERENCE_OK=0
if [[ "$CURL_RC" -eq 0 ]] && grep -q '"choices"' "$RESP"; then
  INFERENCE_OK=1
fi
printf 'curl_rc=%s\ninference_ok=%s\ninference_seconds=%s\n' \
  "$CURL_RC" "$INFERENCE_OK" "$INFERENCE_SECONDS" | \
  sudo -u "$OWNER" -H tee "$OUT/inference-result.txt" >/dev/null

echo
echo '=== INFERENCE RESULT ==='
cat "$OUT/inference-result.txt"
echo
echo '=== RESPONSE ==='
cat "$RESP" 2>/dev/null || true
echo
echo '=== CURL STDERR ==='
cat "$CURLERR" 2>/dev/null || true
echo
echo '=== GPU AFTER INFERENCE (used MiB, free MiB) ==='
cat "$OUT/gpu-after-inference.txt"
echo
echo '=== LOW-VRAM / GENERATION MARKERS AFTER INFERENCE ==='
grep -E 'low-VRAM|persistent cache|cache plan|stage|SSD streaming|chat ctx=|decoding|t/s|memory:|CUDA' "$LOG" | tail -n 160 || true

if [[ "$INFERENCE_OK" -ne 1 ]]; then
  echo "runtime_smoke_inference_failed" >&2
  exit 4
fi

echo
echo "runtime_smoke_ok: http://127.0.0.1:${TEST_PORT} inference_ok=1"
