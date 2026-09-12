#!/usr/bin/env bash
set -euo pipefail

REPO="${REPO:-/home/sibilla-cumana/src/ds4-cuda-stream-pr739}"
SNAPSHOT="${SNAPSHOT:-/home/sibilla-cumana/ds4-sibilla-snapshot-20260912}"
OUT="${OUT:-$SNAPSHOT/low-vram-analysis}"

mkdir -p "$OUT"

patterns='cuda-low-vram-stream|DS4_CUDA_LOW_VRAM_STAGE_MB|LOW_VRAM|low_vram|ssd-streaming-cold|ssd_streaming_cold|stream_all_weights|stream-all-weights|prefill_chunk|prefill-chunk'

# Run git as the repository owner to avoid safe.directory / dubious ownership issues.
sudo -u sibilla-cumana -H env REPO="$REPO" OUT="$OUT" PATTERNS="$patterns" bash -c '
set -euo pipefail

cd "$REPO"

git rev-parse HEAD > "$OUT/head.txt"
git status --short --branch > "$OUT/status.txt"

# All source occurrences that define or consume the low-VRAM/cold-stream path.
grep -RInE "$PATTERNS" \
  Makefile README.md ds4*.c ds4*.h ds4_cuda.cu rocm tests \
  > "$OUT/symbol-hits.txt" || true

# Focused tracked delta against the known PR739 head/checkout HEAD.
git diff HEAD -- \
  Makefile README.md \
  ds4.c ds4.h ds4_agent.c ds4_bench.c ds4_cli.c ds4_cuda.cu \
  ds4_eval.c ds4_gpu.h ds4_help.c ds4_server.c ds4_ssd.c ds4_ssd.h \
  rocm/ds4_rocm_current_api_compat.cuh tests/test_gpu_args_cli.sh \
  > "$OUT/tracked-low-vram-candidates.patch"

# Pickaxe views: smaller diffs centered on the capability names/config knobs.
for needle in \
  cuda-low-vram-stream \
  DS4_CUDA_LOW_VRAM_STAGE_MB \
  ssd-streaming-cold \
  low_vram
  do
    safe="$(printf %s "$needle" | tr -c "A-Za-z0-9_.-" _ )"
    git diff HEAD -S"$needle" -- . > "$OUT/pickaxe-$safe.patch" || true
  done

# Preserve the source-only untracked test if present.
if [[ -f tests/test_ssd.c ]]; then
  cp -a tests/test_ssd.c "$OUT/test_ssd.c"
fi

# Do not copy binaries, model symlinks or logs.
git ls-files --others --exclude-standard > "$OUT/untracked-files.txt"
'

echo '=== LOW-VRAM ANALYSIS ==='
ls -lh "$OUT"
echo
echo '=== SYMBOL HITS ==='
sed -n '1,220p' "$OUT/symbol-hits.txt"
echo
echo '=== PATCH SIZE ==='
wc -l "$OUT"/*.patch
