#!/usr/bin/env bash
set -euo pipefail

PORT_DIR="${PORT_DIR:-/home/sibilla-cumana/src/ds4-main-lowvram-port}"
OWNER="${OWNER:-sibilla-cumana}"
CUDA_SRC="$PORT_DIR/ds4_cuda.cu"

if [[ ! -f "$CUDA_SRC" ]]; then
  echo "missing_source: $CUDA_SRC" >&2
  exit 1
fi

sudo -u "$OWNER" -H python3 - "$CUDA_SRC" <<'PY'
from pathlib import Path
import sys

path = Path(sys.argv[1])
text = path.read_text()

marker = "DS4_LOW_VRAM_NO_FILE_HOST_REGISTER"
if marker in text:
    print("already_patched: DS4 low-VRAM file-backed host registration is disabled")
    raise SystemExit(0)

range_old = """    if (g_model_range_mapping_supported) {\n        const long page_sz_l = sysconf(_SC_PAGESIZE);"""
range_new = """    /* DS4_LOW_VRAM_NO_FILE_HOST_REGISTER\n     * On discrete NVIDIA/Linux, cudaHostRegister/cudaHostUnregister of\n     * file-backed GGUF pages can make the NVIDIA GUP unpin path dirty the\n     * page-cache pages.  In low-VRAM SSD mode we already have an fd-backed\n     * staging path, so never register model-file ranges with CUDA here. */\n    if (g_model_range_mapping_supported && !g_cuda_low_vram_stream) {\n        const long page_sz_l = sysconf(_SC_PAGESIZE);"""

count = text.count(range_old)
if count != 1:
    raise SystemExit(f"unexpected range-registration anchor count: {count} (wanted 1)")
text = text.replace(range_old, range_new, 1)

full_old = """    cudaError_t err = cudaHostRegister((void *)model_map, (size_t)model_size,\n                                       cudaHostRegisterMapped | cudaHostRegisterReadOnly);"""
full_new = """    /* DS4_LOW_VRAM_NO_FILE_HOST_REGISTER\n     * Keep file-backed GGUF mappings out of cudaHostRegister in the bounded\n     * SSD staging mode.  The NVIDIA 535 Linux unpin path can mark pinned\n     * file-cache pages dirty even when registration used the ReadOnly flag. */\n    if (g_cuda_low_vram_stream && g_model_fd >= 0) {\n        fprintf(stderr,\n                \"ds4: CUDA low-VRAM SSD stream: skipping file-backed model host registration\\n\");\n        return 1;\n    }\n\n    cudaError_t err = cudaHostRegister((void *)model_map, (size_t)model_size,\n                                       cudaHostRegisterMapped | cudaHostRegisterReadOnly);"""

count = text.count(full_old)
if count != 2:
    raise SystemExit(f"unexpected full-model registration anchor count: {count} (wanted 2)")
text = text.replace(full_old, full_new)

path.write_text(text)
print("patched:", path)
PY

echo
echo '=== PATCH MARKERS ==='
sudo -u "$OWNER" -H rg -n -C 5 \
  'DS4_LOW_VRAM_NO_FILE_HOST_REGISTER|skipping file-backed model host registration|cudaHostRegister' \
  "$CUDA_SRC"

echo
echo '=== DIFF CHECK ==='
sudo -u "$OWNER" -H git -C "$PORT_DIR" diff --check

echo
echo '=== DIFF SUMMARY ==='
sudo -u "$OWNER" -H git -C "$PORT_DIR" diff --stat

echo
echo 'patch_ok: runtime not started; production untouched'
