#!/usr/bin/env bash
set -euo pipefail

REPO="${REPO:-/home/sibilla-cumana/src/ds4-cuda-stream-pr739}"
SNAPSHOT="${SNAPSHOT:-/home/sibilla-cumana/ds4-sibilla-snapshot-20260912}"
OUT="${OUT:-$SNAPSHOT/upstream-porting}"

sudo install -d -o sibilla-cumana -g sibilla-cumana -m 0775 "$OUT"

sudo -u sibilla-cumana -H env REPO="$REPO" OUT="$OUT" bash -c '
set -euo pipefail

cd "$REPO"

git fetch origin main

LOCAL_HEAD="$(git rev-parse HEAD)"
UPSTREAM_HEAD="$(git rev-parse origin/main)"

printf "%s\n" "$LOCAL_HEAD" > "$OUT/local-head.txt"
printf "%s\n" "$UPSTREAM_HEAD" > "$OUT/upstream-head.txt"

patterns="cuda_low_vram_stream|cuda-low-vram-stream|DS4_CUDA_LOW_VRAM_STAGE_MB|DS4_CUDA_LOW_VRAM_RESERVE_MB|ssd_streaming_cold|ssd-streaming-cold"

# What exists in the deployed dirty working tree.
grep -RInE "$patterns" \
  README.md ds4.c ds4.h ds4_cli.c ds4_server.c ds4_bench.c ds4_eval.c \
  ds4_agent.c ds4_gpu.h ds4_cuda.cu ds4_help.c ds4_ssd.c ds4_ssd.h \
  tests rocm 2>/dev/null > "$OUT/local-symbol-hits.txt" || true

# What exists in current upstream main, without checking it out.
for needle in \
  cuda_low_vram_stream \
  cuda-low-vram-stream \
  DS4_CUDA_LOW_VRAM_STAGE_MB \
  DS4_CUDA_LOW_VRAM_RESERVE_MB \
  ssd_streaming_cold \
  ssd-streaming-cold
  do
    echo "=== $needle ==="
    git grep -n -F "$needle" origin/main -- \
      README.md ds4.c ds4.h ds4_cli.c ds4_server.c ds4_bench.c ds4_eval.c \
      ds4_agent.c ds4_gpu.h ds4_cuda.cu ds4_help.c ds4_ssd.c ds4_ssd.h \
      tests rocm 2>/dev/null || true
    echo
  done > "$OUT/upstream-symbol-hits.txt"

# Local tracked feature delta, restricted to the most relevant files.
git diff HEAD -- \
  README.md ds4.c ds4.h ds4_cli.c ds4_server.c ds4_bench.c ds4_eval.c \
  ds4_agent.c ds4_gpu.h ds4_cuda.cu ds4_help.c ds4_ssd.c ds4_ssd.h \
  tests/test_gpu_args_cli.sh \
  > "$OUT/local-feature-delta.patch"

# Files changed both locally and upstream since the PR739 base.
git diff --name-only HEAD > "$OUT/local-modified-files.txt"
git diff --name-only HEAD..origin/main > "$OUT/upstream-changed-files.txt"
comm -12 \
  <(sort "$OUT/local-modified-files.txt") \
  <(sort "$OUT/upstream-changed-files.txt") \
  > "$OUT/overlap-files.txt"

# Compact upstream divergence for overlapping files only.
if [[ -s "$OUT/overlap-files.txt" ]]; then
  mapfile -t overlap < "$OUT/overlap-files.txt"
  git diff --stat HEAD..origin/main -- "${overlap[@]}" > "$OUT/upstream-overlap-stat.txt"
else
  : > "$OUT/upstream-overlap-stat.txt"
fi

# Focused local hunks containing the low-VRAM implementation names/config.
git diff HEAD -U12 -G"cuda_low_vram_stream|cuda-low-vram-stream|DS4_CUDA_LOW_VRAM_STAGE_MB|DS4_CUDA_LOW_VRAM_RESERVE_MB|low_vram" -- \
  ds4.c ds4.h ds4_cli.c ds4_server.c ds4_bench.c ds4_eval.c ds4_agent.c \
  ds4_gpu.h ds4_cuda.cu ds4_help.c README.md tests/test_gpu_args_cli.sh \
  > "$OUT/local-low-vram-focused.patch" || true
'

echo '=== HEADS ==='
printf 'local:    '; cat "$OUT/local-head.txt"
printf 'upstream: '; cat "$OUT/upstream-head.txt"

echo
echo '=== UPSTREAM SYMBOL HITS ==='
sed -n '1,220p' "$OUT/upstream-symbol-hits.txt"

echo
echo '=== OVERLAP FILES ==='
cat "$OUT/overlap-files.txt"

echo
echo '=== UPSTREAM OVERLAP STAT ==='
cat "$OUT/upstream-overlap-stat.txt"

echo
echo '=== FOCUSED PATCH SIZE ==='
wc -l "$OUT/local-low-vram-focused.patch" "$OUT/local-feature-delta.patch"
