#!/usr/bin/env bash
set -euo pipefail
PORT="${PORT:-/home/sibilla-cumana/src/ds4-main-lowvram-port}"
SNAPSHOT="${SNAPSHOT:-/home/sibilla-cumana/ds4-sibilla-snapshot-20260912}"
OUT="${OUT:-$SNAPSHOT/upstream-porting/build-break-inspection}"

sudo -u sibilla-cumana -H mkdir -p "$OUT"

sudo -u sibilla-cumana -H env PORT="$PORT" OUT="$OUT" python3 - <<'PY'
from pathlib import Path
import os,re
root=Path(os.environ['PORT']); out=Path(os.environ['OUT'])

def block(path, pattern, before=25, after=80):
    p=root/path; lines=p.read_text().splitlines()
    rx=re.compile(pattern)
    hits=[i for i,l in enumerate(lines) if rx.search(l)]
    data=[]
    for i in hits:
        a=max(0,i-before); b=min(len(lines),i+after+1)
        data.append(f'===== {path}:{i+1} match {pattern} =====')
        data += [f'{j+1:6d}: {lines[j]}' for j in range(a,b)]
    return '\n'.join(data)+'\n'

text=''
for path,pat,b,a in [
 ('ds4.c',r'^static int generate_glm_metal_argmax\(',8,55),
 ('ds4.c',r'^static int generate_metal_graph_raw_swa\(',8,75),
 ('ds4.c',r'return generate_glm_metal_argmax\(',20,45),
 ('ds4.c',r'return generate_metal_graph_raw_swa\(',20,45),
 ('ds4_cuda.cu',r'^static uint64_t cuda_model_cache_limit_bytes\(',30,120),
 ('ds4_cuda.cu',r'^extern "C" void ds4_gpu_set_cuda_low_vram_stream\(',20,60),
 ('ds4_cuda.cu',r'cuda_model_cache_limit_reset',20,30),
 ('ds4_cuda.cu',r'g_model_default_cache_limit',15,40),
 ('ds4_cuda.cu',r'cuda_model_range_ptr_from_fd',20,80),
]:
    text += block(path,pat,b,a)
(out/'blocks.txt').write_text(text)
PY

sudo -u sibilla-cumana -H sh -c '
cd "$1"
{
 echo "=== HEAD ==="; git rev-parse HEAD
 echo "=== STATUS ==="; git status --short --branch
 echo "=== DEFINITIONS / CALLS ==="
 grep -nE "generate_glm_metal_argmax|generate_metal_graph_raw_swa|cuda_model_cache_limit_bytes|cuda_model_cache_limit_reset|g_model_default_cache_limit|ds4_gpu_set_cuda_low_vram_stream|cuda_model_range_ptr_from_fd" ds4.c ds4_cuda.cu || true
 echo "=== REJECT CUDA ==="
 cat ds4_cuda.cu.rej 2>/dev/null || true
} > "$2/summary.txt"
' sh "$PORT" "$OUT"

echo '=== SUMMARY ==='
cat "$OUT/summary.txt"
echo
echo '=== SOURCE BLOCKS ==='
cat "$OUT/blocks.txt"
