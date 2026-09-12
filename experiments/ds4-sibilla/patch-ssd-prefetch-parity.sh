#!/usr/bin/env bash
set -euo pipefail

PORT_DIR="${PORT_DIR:-/home/sibilla-cumana/src/ds4-main-lowvram-port}"
OWNER="${OWNER:-sibilla-cumana}"
SRC="$PORT_DIR/ds4_cuda.cu"

if [[ ! -f "$SRC" ]]; then
  echo "missing_source: $SRC" >&2
  exit 1
fi

sudo -u "$OWNER" -H python3 - "$SRC" <<'PY'
from pathlib import Path
import sys

path = Path(sys.argv[1])
text = path.read_text()
marker = "DS4_SSD_PREFETCH_PARITY"

if marker in text:
    print("already_patched: SSD streaming prefetch suppression present")
    raise SystemExit(0)

old = """static int cuda_model_prefetch_range(const void *model_map, uint64_t model_size, uint64_t map_offset, uint64_t map_size) {
    if (!model_map || map_size == 0 || map_offset > model_size || map_size > model_size - map_offset) return 0;
    if (getenv(\"DS4_CUDA_NO_MODEL_PREFETCH\") != NULL ||"""
new = """static int cuda_model_prefetch_range(const void *model_map, uint64_t model_size, uint64_t map_offset, uint64_t map_size) {
    if (!model_map || map_size == 0 || map_offset > model_size || map_size > model_size - map_offset) return 0;
    /* DS4_SSD_PREFETCH_PARITY
     * Production suppresses CUDA model prefetch while SSD streaming is active.
     * Without this guard the modern port can materialize extra model ranges on
     * device at startup, defeating the intended cold/streamed residency model. */
    if (g_ssd_streaming_mode) return 0;
    if (getenv(\"DS4_CUDA_NO_MODEL_PREFETCH\") != NULL ||"""

count = text.count(old)
if count != 1:
    raise SystemExit(f"unexpected cuda_model_prefetch_range anchor count: {count} (wanted 1)")

path.write_text(text.replace(old, new, 1))
print("patched:", path)
PY

echo
echo '=== PREFETCH PARITY MARKER ==='
sudo -u "$OWNER" -H rg -n -C 8 \
  'DS4_SSD_PREFETCH_PARITY|cuda_model_prefetch_range|g_ssd_streaming_mode' \
  "$SRC" | head -120

echo
echo '=== DIFF CHECK ==='
sudo -u "$OWNER" -H git -C "$PORT_DIR" diff --check

echo
echo '=== DIFF SUMMARY ==='
sudo -u "$OWNER" -H git -C "$PORT_DIR" diff --stat

echo
echo 'prefetch_parity_patch_ok: runtime not started; production untouched'
