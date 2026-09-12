#!/usr/bin/env bash
set -euo pipefail

PORT="${PORT:-/home/sibilla-cumana/src/ds4-main-lowvram-port}"
OWNER="${OWNER:-sibilla-cumana}"
TEST_PORT="${TEST_PORT:-19195}"
CUDA_FILE="$PORT/ds4_cuda.cu"

if [[ ! -f "$CUDA_FILE" ]]; then
  echo "missing_cuda_file: $CUDA_FILE" >&2
  exit 1
fi

if [[ ! -x "$PORT/ds4-server" ]]; then
  echo "missing_built_server: $PORT/ds4-server" >&2
  exit 1
fi

if ss -ltn | awk '{print $4}' | grep -qE "[:.]${TEST_PORT}$"; then
  echo "unsafe_test_port_busy: $TEST_PORT" >&2
  exit 1
fi

MARKERS="$(grep -c 'DS4_LOW_VRAM_NO_FILE_HOST_REGISTER' "$CUDA_FILE" || true)"
WHOLE_GUARDS="$(grep -c 'if (g_cuda_low_vram_stream && g_model_fd >= 0)' "$CUDA_FILE" || true)"
RANGE_GUARDS="$(grep -c 'g_model_range_mapping_supported && !g_cuda_low_vram_stream' "$CUDA_FILE" || true)"

printf 'markers=%s\nwhole_model_guards=%s\nrange_guards=%s\n' \
  "$MARKERS" "$WHOLE_GUARDS" "$RANGE_GUARDS"

if [[ "$MARKERS" -lt 3 || "$WHOLE_GUARDS" -lt 2 || "$RANGE_GUARDS" -lt 1 ]]; then
  echo "unsafe_source_guard_missing" >&2
  exit 2
fi

echo
echo '=== GUARDED CALL SITES ==='
grep -n -C 5 -E \
  'DS4_LOW_VRAM_NO_FILE_HOST_REGISTER|cudaHostRegister\(|cudaHostUnregister\(' \
  "$CUDA_FILE" | sed -n '1,260p'

echo
echo '=== DIFF CHECK ==='
sudo -u "$OWNER" -H git -C "$PORT" diff --check

echo
echo '=== BUILD ARTIFACT ==='
ls -lh "$PORT/ds4-server"
file "$PORT/ds4-server"

echo
echo '=== MEMORY / DIRTY ==='
grep -E '^(MemAvailable|Dirty|Writeback|SwapTotal|SwapFree):' /proc/meminfo
DIRTY_KB="$(awk '/^Dirty:/ {print $2}' /proc/meminfo)"
WRITEBACK_KB="$(awk '/^Writeback:/ {print $2}' /proc/meminfo)"

# Refuse even a future runtime smoke if the host is already carrying a large
# dirty/writeback backlog.  The previous incident reached ~50 GiB Dirty.
if [[ "$DIRTY_KB" -gt 524288 || "$WRITEBACK_KB" -gt 524288 ]]; then
  echo "unsafe_dirty_backlog: Dirty=${DIRTY_KB}kB Writeback=${WRITEBACK_KB}kB" >&2
  exit 3
fi

echo
echo '=== CURRENT-BOOT EXT4/NVME WARNINGS ==='
if sudo -n true 2>/dev/null; then
  sudo dmesg -T | grep -Ei \
    'mpage_prepare_extent_to_map|EXT4-fs warning|I/O error|nvme.*(timeout|reset|abort)' \
    | tail -80 || true
else
  echo 'sudo_noninteractive_unavailable: skipped dmesg check'
fi

echo
echo '=== PRODUCTION PRESENCE (READ ONLY) ==='
systemctl is-active bottazzi-ds4.service || true
ss -ltn | grep -E ':19194|:19195' || true

echo
echo 'SAFE_STATIC_OK: low-VRAM file-backed host registration guards present; no runtime started'
