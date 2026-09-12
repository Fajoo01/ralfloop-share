#!/usr/bin/env bash
set -euo pipefail

PORT="${PORT:-/home/sibilla-cumana/src/ds4-main-lowvram-port}"
SNAPSHOT="${SNAPSHOT:-/home/sibilla-cumana/ds4-sibilla-snapshot-20260912}"
OUT="${OUT:-$SNAPSHOT/upstream-porting/reject-analysis}"

if [[ ! -d "$PORT/.git" ]]; then
  echo "missing_port_clone: $PORT" >&2
  exit 1
fi

# Create analysis output with the repository owner, because the current bandi
# session may not yet have refreshed its supplementary groups.
sudo -u sibilla-cumana -H mkdir -p "$OUT"

sudo -u sibilla-cumana -H env PORT="$PORT" OUT="$OUT" bash -c '
set -euo pipefail
cd "$PORT"

: > "$OUT/reject-context.txt"
: > "$OUT/rejected-additions.txt"
: > "$OUT/reject-counts.txt"
: > "$OUT/ported-symbol-inventory.txt"

shopt -s nullglob
rejects=( *.rej tests/*.rej rocm/*.rej )
printf "%s\n" "${rejects[@]}" > "$OUT/reject-files.txt"

for rej in "${rejects[@]}"; do
    target="${rej%.rej}"
    hunks="$(grep -c '^@@' "$rej" || true)"
    printf "%s\t%s\n" "$rej" "$hunks" >> "$OUT/reject-counts.txt"

    {
      echo
      echo "================================================================"
      echo "REJECT: $rej"
      echo "TARGET: $target"
      echo "HUNKS:  $hunks"
      echo "================================================================"
      cat "$rej"
      echo
      echo "---------------- CURRENT TARGET CONTEXT ----------------"
    } >> "$OUT/reject-context.txt"

    # Capture only proposed added lines for quick semantic review.
    awk -v file="$rej" '
      /^\+\+\+/ {next}
      /^\+/ {print file ": " substr($0,2)}
    ' "$rej" >> "$OUT/rejected-additions.txt"

    if [[ -f "$target" ]]; then
      # For every rejected hunk, print the current-file area around its proposed
      # new-file line. This is read-only and makes manual adaptation much easier.
      while read -r start; do
        [[ -n "$start" ]] || continue
        lo=$(( start > 18 ? start - 18 : 1 ))
        hi=$(( start + 28 ))
        {
          echo
          echo "### $target around line $start ($lo..$hi)"
          nl -ba "$target" | sed -n "${lo},${hi}p"
        } >> "$OUT/reject-context.txt"
      done < <(sed -nE 's/^@@ -[0-9]+(,[0-9]+)? \+([0-9]+)(,[0-9]+)? @@.*/\2/p' "$rej")
    fi

done

# Inventory what already landed despite the rejects.
grep -RInE \
  "cuda_low_vram_stream|cuda-low-vram-stream|DS4_CUDA_LOW_VRAM_(STAGE|RESERVE)_MB|ds4_gpu_set_cuda_low_vram_stream|low_vram_stage" \
  ds4.c ds4.h ds4_agent.c ds4_bench.c ds4_cli.c ds4_cuda.cu ds4_eval.c \
  ds4_gpu.h ds4_help.c ds4_server.c ds4_ssd.c ds4_ssd.h tests 2>/dev/null \
  > "$OUT/ported-symbol-inventory.txt" || true

git status --short --branch > "$OUT/status.txt"
git diff --stat > "$OUT/diff-stat.txt"
git diff > "$OUT/current-applied.patch"

{
  echo "head=$(git rev-parse HEAD)"
  echo "reject_files=${#rejects[@]}"
  echo "reject_hunks=$(awk '{s+=$2} END {print s+0}' "$OUT/reject-counts.txt")"
  echo "applied_diff_lines=$(wc -l < "$OUT/current-applied.patch")"
} > "$OUT/summary.txt"
'

echo '=== REJECT ANALYSIS SUMMARY ==='
cat "$OUT/summary.txt"
echo
echo '=== REJECT COUNTS ==='
cat "$OUT/reject-counts.txt"
echo
echo '=== REJECTED ADDITIONS (first 220 lines) ==='
sed -n '1,220p' "$OUT/rejected-additions.txt"
echo
echo '=== PORTED SYMBOL INVENTORY (first 220 lines) ==='
sed -n '1,220p' "$OUT/ported-symbol-inventory.txt"
echo
echo '=== OUTPUT ==='
ls -lh "$OUT"
