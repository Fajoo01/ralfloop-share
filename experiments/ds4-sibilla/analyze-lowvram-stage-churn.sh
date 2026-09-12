#!/usr/bin/env bash
set -euo pipefail

MODERN="${MODERN:-/home/sibilla-cumana/src/ds4-main-lowvram-port}"
PROD="${PROD:-/home/sibilla-cumana/src/ds4-cuda-stream-pr739}"
OWNER="${OWNER:-sibilla-cumana}"
OUT="${OUT:-/home/sibilla-cumana/ds4-sibilla-snapshot-20260912/upstream-porting/stage-churn-analysis}"

mkdir -p "$OUT"
for d in "$MODERN" "$PROD"; do
  [[ -f "$d/ds4_cuda.cu" ]] || { echo "missing ds4_cuda.cu under $d" >&2; exit 1; }
done

PATTERN='low-VRAM staging|stage_range|stage_used|stage_bytes|staging epoch|reuses=|persistent-cache|g_model_range_bytes|model_range_ptr_from_fd|stream_selected|cache_limit|cuda_model_cache|NO_FD_CACHE|ssd_streaming_mode'

sudo -u "$OWNER" -H rg -n -C 18 "$PATTERN" "$MODERN/ds4_cuda.cu" > "$OUT/modern.txt" || true
sudo -u "$OWNER" -H rg -n -C 18 "$PATTERN" "$PROD/ds4_cuda.cu" > "$OUT/production.txt" || true
diff -u "$OUT/production.txt" "$OUT/modern.txt" > "$OUT/production-vs-modern.diff" || true

echo '=== MODERN STAGE CORE ==='
sudo -u "$OWNER" -H rg -n -C 28 \
  'cuda_low_vram_stage_range|staging epoch reset|g_low_vram|stage.*used|reuses=|persistent-cache' \
  "$MODERN/ds4_cuda.cu" || true

echo
echo '=== MODERN STAGE CALLERS ==='
sudo -u "$OWNER" -H rg -n -C 14 \
  'cuda_low_vram_stage_range\(|cuda_model_range_ptr_from_fd\(|cuda_model_range_ptr\(' \
  "$MODERN/ds4_cuda.cu" || true

echo
echo '=== PRODUCTION STAGE CORE ==='
sudo -u "$OWNER" -H rg -n -C 28 \
  'cuda_low_vram_stage_range|staging epoch reset|g_low_vram|stage.*used|reuses=|persistent-cache' \
  "$PROD/ds4_cuda.cu" || true

echo
echo '=== CACHE / STREAM-SELECTED DELTAS ==='
grep -E -C 12 \
  'stream_selected|persistent.cache|cache_limit|model_range_bytes|stage_range|staging epoch|NO_FD_CACHE' \
  "$OUT/production-vs-modern.diff" | head -900 || true

echo
echo '=== STATIC COUNTS ==='
for label in modern production; do
  file="$OUT/$label.txt"
  printf '%s stage_range_refs=' "$label"
  grep -c 'cuda_low_vram_stage_range' "$file" || true
  printf '%s epoch_reset_refs=' "$label"
  grep -c 'staging epoch reset' "$file" || true
  printf '%s stream_selected_refs=' "$label"
  grep -c 'stream_selected' "$file" || true
  printf '%s model_range_bytes_refs=' "$label"
  grep -c 'g_model_range_bytes' "$file" || true
done

echo
echo '=== SAFETY STATE ==='
grep -E '^(MemAvailable|Dirty|Writeback):' /proc/meminfo
printf 'production_19194='; ss -ltn | grep -q ':19194 ' && echo yes || echo no
printf 'test_19195='; ss -ltn | grep -q ':19195 ' && echo yes || echo no

echo
echo '=== OUTPUT ==='
ls -lh "$OUT"

echo
echo 'STAGE_CHURN_ANALYZE_OK: static source analysis only; no DS4 runtime started or stopped; production untouched'
