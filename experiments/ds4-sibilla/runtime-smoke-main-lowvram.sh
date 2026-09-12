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
printf 'gpu_used_mib=%s\ngpu_free_mib=%s\nmin_free_mib=%s\n' "$USED_MIB" "$FREE_MIB" "$MIN_FREE_MIB" | sudo -u "$OWNER" -H tee "$OUT/gpu-before.txt" >/dev/null

echo '=== GPU BEFORE ==='
cat "$OUT/gpu-before.txt"

if [[ "$FREE_MIB" -lt "$MIN_FREE_MIB" ]]; then
  echo "insufficient_free_vram_for_safe_smoke: ${FREE_MIB} MiB < ${MIN_FREE_MIB} MiB" >&2
  echo "No runtime process started."
  exit 2
fi

: | sudo -u "$OWNER" -H tee "$LOG" >/dev/null

cleanup() {
  if [[ -f "$PIDFILE" ]]; then
    pid="$(cat "$PIDFILE" 2>/dev/null || true)"
    if [[ -n "${pid:-}" ]] && kill -0 "$pid" 2>/dev/null; then
      kill "$pid" 2>/dev/null || true
      for _ in $(seq 1 20); do
        kill -0 "$pid" 2>/dev/null || break
        sleep 0.25
      done
      kill -KILL "$pid" 2>/dev/null || true
    fi
  fi
}
trap cleanup EXIT INT TERM

sudo -u "$OWNER" -H sh -c '
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
' sh "$BIN" "$MODEL" "$TEST_PORT" "$LOG" | sudo -u "$OWNER" -H tee "$PIDFILE" >/dev/null

PID="$(cat "$PIDFILE")"
echo "runtime_pid=$PID"

READY=0
for i in $(seq 1 80); do
  if ! kill -0 "$PID" 2>/dev/null; then
    break
  fi
  if ss -ltn | awk '{print $4}' | grep -qE "[:.]${TEST_PORT}$"; then
    READY=1
    break
  fi
  sleep 0.5
done

printf 'ready=%s\npid=%s\nport=%s\n' "$READY" "$PID" "$TEST_PORT" | sudo -u "$OWNER" -H tee "$OUT/result.txt" >/dev/null

nvidia-smi --query-gpu=memory.used,memory.free --format=csv,noheader,nounits | head -n1 | \
  sudo -u "$OWNER" -H tee "$OUT/gpu-during.txt" >/dev/null

echo
echo '=== RESULT ==='
cat "$OUT/result.txt"
echo
echo '=== GPU DURING (used MiB, free MiB) ==='
cat "$OUT/gpu-during.txt"
echo
echo '=== SERVER LOG TAIL ==='
tail -n 120 "$LOG" || true

echo
echo '=== LOW-VRAM STARTUP MARKERS ==='
grep -E 'low-VRAM|persistent cache|stage|SSD streaming|listening on|context buffers|memory:' "$LOG" || true

if [[ "$READY" -ne 1 ]]; then
  echo "runtime_smoke_failed_before_listen" >&2
  exit 3
fi

echo
echo "runtime_smoke_ok: http://127.0.0.1:${TEST_PORT}"
