#!/usr/bin/env bash
set -euo pipefail

MODERN="${MODERN:-/home/sibilla-cumana/src/ds4-main-lowvram-port}"
PROD="${PROD:-/home/sibilla-cumana/src/ds4-cuda-stream-pr739}"
OWNER="${OWNER:-sibilla-cumana}"

show_report() {
  local label="$1" dir="$2" file="$2/ds4.c"
  echo "=== $label memory report function ==="
  local line
  line="$(sudo -u "$OWNER" -H rg -n -m1 '\+ buffers %.2f GiB \+ resident model %.2f GiB' "$file" | cut -d: -f1 || true)"
  if [[ -z "$line" ]]; then
    echo "memory report format not found in $file" >&2
    return 1
  fi
  local start=$(( line > 120 ? line - 120 : 1 ))
  local end=$(( line + 120 ))
  sudo -u "$OWNER" -H sed -n "${start},${end}p" "$file" | nl -ba -v "$start"
  echo

  echo "=== $label component assignments/references ==="
  sudo -u "$OWNER" -H rg -n -C 10 \
    'startup_model_span_bytes|dynamic_expert_cache_bytes|ssd_streaming_full_layer_bytes|expert_reserved_bytes|resident model|ds4_add_sat_u64\(total' \
    "$file" || true
  echo
}

show_report MODERN "$MODERN"
show_report PRODUCTION "$PROD"

echo '=== STATIC COMPONENT DIFF ==='
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
sudo -u "$OWNER" -H rg -n -C 8 \
  'startup_model_span_bytes|dynamic_expert_cache_bytes|ssd_streaming_full_layer_bytes|expert_reserved_bytes|resident model|ds4_add_sat_u64\(total' \
  "$PROD/ds4.c" > "$tmp/prod.txt" || true
sudo -u "$OWNER" -H rg -n -C 8 \
  'startup_model_span_bytes|dynamic_expert_cache_bytes|ssd_streaming_full_layer_bytes|expert_reserved_bytes|resident model|ds4_add_sat_u64\(total' \
  "$MODERN/ds4.c" > "$tmp/modern.txt" || true
diff -u "$tmp/prod.txt" "$tmp/modern.txt" | head -700 || true

echo
echo '=== SAFETY STATE ==='
grep -E '^(MemAvailable|Dirty|Writeback):' /proc/meminfo
printf 'production_19194='; ss -ltn | grep -q ':19194 ' && echo yes || echo no
printf 'test_19195='; ss -ltn | grep -q ':19195 ' && echo yes || echo no

echo
echo 'MEMORY_COMPONENT_INSPECT_OK: static source inspection only; no DS4 runtime started or stopped; production untouched'
