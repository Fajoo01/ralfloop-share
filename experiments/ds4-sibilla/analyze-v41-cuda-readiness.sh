#!/usr/bin/env bash
set -euo pipefail

SOURCE="${SOURCE:-/home/sibilla-cumana/src/ds4-main-lowvram-port}"
PRODUCTION_SOURCE="${PRODUCTION_SOURCE:-/home/sibilla-cumana/src/ds4-cuda-stream-pr739}"
PROD_PORT="${PROD_PORT:-19194}"
TEST_PORT="${TEST_PORT:-19195}"

HEADER="$SOURCE/ds4_gpu.h"
CUDA="$SOURCE/ds4_cuda.cu"
METAL="$SOURCE/ds4_metal.m"
CORE="$SOURCE/ds4.c"
BINARY="$SOURCE/ds4-server"

for file in "$HEADER" "$CUDA" "$METAL" "$CORE" "$BINARY"; do
    [[ -e "$file" ]] || { echo "missing: $file" >&2; exit 1; }
done

tmp="$(mktemp -d)"
trap 'rm -f "$tmp/required" "$tmp/metal" "$tmp/cuda" "$tmp/missing-cuda"; rmdir "$tmp"' EXIT
{ grep -oE 'ds4_gpu_dsv41_[A-Za-z0-9_]+' "$HEADER" || true; } | sort -u > "$tmp/required"
{ grep -oE 'ds4_gpu_dsv41_[A-Za-z0-9_]+' "$METAL" || true; } | sort -u > "$tmp/metal"
{ grep -oE 'ds4_gpu_dsv41_[A-Za-z0-9_]+' "$CUDA" || true; } | sort -u > "$tmp/cuda"
comm -23 "$tmp/required" "$tmp/cuda" > "$tmp/missing-cuda"

required="$(wc -l < "$tmp/required")"
metal="$(wc -l < "$tmp/metal")"
cuda="$(wc -l < "$tmp/cuda")"
missing="$(wc -l < "$tmp/missing-cuda")"
graph_metal_guard="$(grep -c 'DeepSeek V4.1 Metal Graph' "$CORE" || true)"
runtime_metal_reject="$(grep -c 'V4.1 requires Metal inference' "$CORE" || true)"
binary_v41_symbols="$(nm -C "$BINARY" 2>/dev/null | grep -c 'ds4_gpu_dsv41_' || true)"
sm75_cubins="$(cuobjdump --list-elf "$BINARY" 2>/dev/null | grep -c '\.sm_75\.cubin' || true)"
host_register_guards="$(grep -c 'DS4_LOW_VRAM_NO_FILE_HOST_REGISTER' "$CUDA" || true)"
prod_active="$(systemctl is-active bottazzi-ds4.service || true)"
prod_listening="$(ss -ltn | awk '{print $4}' | grep -cE "[:.]${PROD_PORT}$" || true)"
test_listening="$(ss -ltn | awk '{print $4}' | grep -cE "[:.]${TEST_PORT}$" || true)"

printf 'source_head=%s\n' "$(git -C "$SOURCE" rev-parse HEAD)"
printf 'required_v41_gpu_apis=%s\nmetal_v41_gpu_apis=%s\ncuda_v41_gpu_apis=%s\nmissing_cuda_apis=%s\n' \
    "$required" "$metal" "$cuda" "$missing"
printf 'v41_graph_metal_guard=%s\nruntime_metal_reject=%s\nbinary_v41_gpu_symbols=%s\n' \
    "$graph_metal_guard" "$runtime_metal_reject" "$binary_v41_symbols"
printf 'sm75_cubins=%s\nhost_register_guards=%s\n' "$sm75_cubins" "$host_register_guards"
printf 'production_service=%s\nproduction_port=%s\ntest_port=%s\n' \
    "$prod_active" "$prod_listening" "$test_listening"
printf 'production_cuda_sha256=%s\n' "$(sha256sum "$PRODUCTION_SOURCE/ds4_cuda.cu" | awk '{print $1}')"
echo 'missing_cuda_api_names:'
sed 's/^/  /' "$tmp/missing-cuda"

if [[ "$required" -eq 0 || "$missing" -ne 0 || "$runtime_metal_reject" -ne 0 ||
      "$binary_v41_symbols" -eq 0 ]]; then
    echo 'V41_CUDA_BLOCKED: upstream graph and GPU primitives are Metal-only'
    exit 2
fi

echo 'V41_CUDA_STATIC_READY'
