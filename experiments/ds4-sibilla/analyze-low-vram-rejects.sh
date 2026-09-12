#!/usr/bin/env bash
set -euo pipefail

PORT="${PORT:-/home/sibilla-cumana/src/ds4-main-lowvram-port}"
SNAPSHOT="${SNAPSHOT:-/home/sibilla-cumana/ds4-sibilla-snapshot-20260912}"
OUT="${OUT:-$SNAPSHOT/upstream-porting/reject-analysis}"

if [[ ! -d "$PORT/.git" ]]; then
  echo "missing_port_clone: $PORT" >&2
  exit 1
fi

# The current bandi login may not yet contain refreshed supplementary groups.
sudo -u sibilla-cumana -H mkdir -p "$OUT"

# Use a quoted heredoc instead of a single-quoted `bash -c` payload: the
# analyzer itself contains awk/sed programs with single quotes.
sudo -u sibilla-cumana -H env PORT="$PORT" OUT="$OUT" bash <<'INNER'
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

    awk -v file="$rej" '
      /^\+\+\+/ {next}
      /^\+/ {print file ": " substr($0,2)}
    ' "$rej" >> "$OUT/rejected-additions.txt"

    if [[ -f "$target" ]]; then
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

grep -RInE \
  "cuda_low_vram_stream|cuda-low-vram-stream|DS4_CUDA_LOW_VRAM_(STAGE|RESERVE)_MB|ds4_gpu_set_cuda_low_vram_stream|low_vram_stage" \
  ds4.c ds4.h ds4_agent.c ds4_bench.c ds4_cli.c ds4_cuda.cu ds4_eval.c \
  ds4_gpu.h ds4_help.c ds4_server.c ds4_ssd.c ds4_ssd.h tests 2>/dev/null \
  > "$OUT/ported-symbol-inventory.txt" || true

git status --short --branch > "$OUT/status.txt"
git diff --stat > "$OUT/diff-stat.txt"
git diff > "$OUT/current-applied.patch"

reject_hunks="$(awk '{s+=$2} END {print s+0}' "$OUT/reject-counts.txt")"
applied_diff_lines="$(wc -l < "$OUT/current-applied.patch")"
{
  echo "head=$(git rev-parse HEAD)"
  echo "reject_files=${#rejects[@]}"
  echo "reject_hunks=$reject_hunks"
  echo "applied_diff_lines=$applied_diff_lines"
} > "$OUT/summary.txt"
INNER

echo '=== REJECT ANALYSIS SUMMARY ==='
sudo -u sibilla-cumana cat "$OUT/summary.txt"
echo
echo '=== REJECT COUNTS ==='
sudo -u sibilla-cumana cat "$OUT/reject-counts.txt"
echo
echo '=== REJECTED ADDITIONS (first 220 lines) ==='
sudo -u sibilla-cumana sed -n '1,220p' "$OUT/rejected-additions.txt"
echo
echo '=== PORTED SYMBOL INVENTORY (first 220 lines) ==='
sudo -u sibilla-cumana sed -n '1,220p' "$OUT/ported-symbol-inventory.txt"
echo
echo '=== OUTPUT ==='
sudo -u sibilla-cumana ls -lh "$OUT"
