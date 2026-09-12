#!/usr/bin/env bash
set -euo pipefail

MODERN="${MODERN:-/home/sibilla-cumana/src/ds4-main-lowvram-port}"
PROD="${PROD:-/home/sibilla-cumana/src/ds4-cuda-stream-pr739}"
OWNER="${OWNER:-sibilla-cumana}"
OUT="${OUT:-/home/sibilla-cumana/ds4-sibilla-snapshot-20260912/upstream-porting/resident-model-accounting}"

mkdir -p "$OUT"

for d in "$MODERN" "$PROD"; do
  [[ -f "$d/ds4.c" && -f "$d/ds4_cuda.cu" ]] || {
    echo "missing source under $d" >&2
    exit 1
  }
done

pattern='resident model|model_bytes|resident_model|resident_bytes|model map|tensor span|initial cuda model map|ds4_gpu_set_model_map_range|ds4_gpu_set_aux_model_map_range|ds4_gpu_register_model_map_no_copy|model_map_range|map_size|model_size'

sudo -u "$OWNER" -H rg -n -C 18 "$pattern" \
  "$MODERN/ds4.c" "$MODERN/ds4_cuda.cu" > "$OUT/modern.txt" || true
sudo -u "$OWNER" -H rg -n -C 18 "$pattern" \
  "$PROD/ds4.c" "$PROD/ds4_cuda.cu" > "$OUT/production.txt" || true

diff -u "$OUT/production.txt" "$OUT/modern.txt" > "$OUT/production-vs-modern.diff" || true

echo '=== MODERN MEMORY-PLAN / RESIDENCY ==='
sudo -u "$OWNER" -H rg -n -C 28 \
  'resident model|model_bytes|initial cuda model map|tensor span' \
  "$MODERN/ds4.c" "$MODERN/ds4_cuda.cu" || true

echo
echo '=== PRODUCTION MEMORY-PLAN / RESIDENCY ==='
sudo -u "$OWNER" -H rg -n -C 28 \
  'resident model|model_bytes|initial cuda model map|tensor span' \
  "$PROD/ds4.c" "$PROD/ds4_cuda.cu" || true

echo
echo '=== MODERN MODEL-MAP CALLERS ==='
sudo -u "$OWNER" -H rg -n -C 14 \
  'ds4_gpu_set_model_map_range|ds4_gpu_set_aux_model_map_range|ds4_gpu_register_model_map_no_copy' \
  "$MODERN/ds4.c" "$MODERN/ds4_cuda.cu" || true

echo
echo '=== PRODUCTION MODEL-MAP CALLERS ==='
sudo -u "$OWNER" -H rg -n -C 14 \
  'ds4_gpu_set_model_map_range|ds4_gpu_set_aux_model_map_range|ds4_gpu_register_model_map_no_copy' \
  "$PROD/ds4.c" "$PROD/ds4_cuda.cu" || true

echo
echo '=== FOCUSED DIFF: MEMORY ACCOUNTING ==='
grep -E -C 12 \
  'resident model|model_bytes|initial cuda model map|tensor span|set_model_map_range|register_model_map' \
  "$OUT/production-vs-modern.diff" | head -500 || true

echo
echo '=== SAFETY STATE ==='
grep -E '^(MemAvailable|Dirty|Writeback):' /proc/meminfo
printf 'production_19194='; ss -ltn | grep -q ':19194 ' && echo yes || echo no
printf 'test_19195='; ss -ltn | grep -q ':19195 ' && echo yes || echo no

echo
echo '=== OUTPUT ==='
ls -lh "$OUT"

echo
echo 'ANALYZE_ACCOUNTING_OK: static comparison only; no DS4 runtime started or stopped; production untouched'
