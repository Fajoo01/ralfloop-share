#!/usr/bin/env bash
set -euo pipefail

PORT="${PORT:-/home/sibilla-cumana/src/ds4-main-lowvram-port}"
PROD="${PROD:-/home/sibilla-cumana/src/ds4-cuda-stream-pr739}"
OWNER="${OWNER:-sibilla-cumana}"
OUT="${OUT:-/home/sibilla-cumana/ds4-sibilla-snapshot-20260912/upstream-porting/residency-gap}"

sudo -u "$OWNER" -H mkdir -p "$OUT"

for d in "$PORT" "$PROD"; do
  [[ -d "$d" ]] || { echo "missing_dir: $d" >&2; exit 1; }
done

capture() {
  local root="$1" label="$2"
  sudo -u "$OWNER" -H sh -c '
    cd "$1"
    {
      echo "=== HEAD / STATUS ==="
      git rev-parse HEAD
      git status --short --branch
      echo
      echo "=== RESIDENCY / CACHE / INITIAL MAP SYMBOLS ==="
      rg -n -C 8 "resident model|initial cuda model map|initial.*model map|model map restricted|g_model_range_bytes|g_model_cache|cache_limit|persistent cache|persistent-cache|cuda_model_prefetch_range|cuda_model_copy_chunked|cuda_model_range_ptr_from_fd|cuda_low_vram_stage_range|ds4_gpu_set_model_map|ds4_gpu_register_model_map_no_copy" ds4.c ds4_cuda.cu ds4_gpu.h ds4_server.c 2>/dev/null || true
      echo
      echo "=== LOW-VRAM BRANCH POINTS ==="
      rg -n -C 6 "g_cuda_low_vram_stream|cuda-low-vram-stream|ssd_streaming_cold|ssd-streaming-cold" ds4.c ds4_cuda.cu ds4_server.c ds4_cli.c ds4_gpu.h 2>/dev/null || true
    } > "$2"
  ' sh "$root" "$OUT/$label.txt"
}

capture "$PORT" modern
capture "$PROD" production

sudo -u "$OWNER" -H diff -u "$OUT/production.txt" "$OUT/modern.txt" > "$OUT/production-vs-modern.diff" || true

echo '=== MODERN KEY LINES ==='
grep -E 'resident model|initial cuda model map|cache plan|persistent cache|persistent-cache|g_model_range_bytes|g_model_cache|cache_limit' "$OUT/modern.txt" | head -n 220 || true

echo
echo '=== PRODUCTION KEY LINES ==='
grep -E 'resident model|initial cuda model map|cache plan|persistent cache|persistent-cache|g_model_range_bytes|g_model_cache|cache_limit' "$OUT/production.txt" | head -n 220 || true

echo
echo '=== FOCUSED DIFF ==='
grep -E -C 4 '^[-+].*(resident model|initial cuda model map|cache plan|persistent cache|persistent-cache|g_model_range_bytes|g_model_cache|cache_limit|cuda_model_prefetch_range|cuda_model_copy_chunked|cuda_model_range_ptr_from_fd|cuda_low_vram_stage_range)' "$OUT/production-vs-modern.diff" | head -n 320 || true

echo
echo '=== SAFETY STATE ==='
grep -E '^(Dirty|Writeback|MemAvailable):' /proc/meminfo
printf 'production_19194='; ss -ltn | awk '{print $4}' | grep -qE '[:.]19194$' && echo yes || echo no
printf 'test_19195='; ss -ltn | awk '{print $4}' | grep -qE '[:.]19195$' && echo yes || echo no

echo
echo '=== OUTPUT ==='
ls -lh "$OUT"

echo
echo 'ANALYZE_ONLY_OK: no DS4 runtime started or stopped; production untouched'
