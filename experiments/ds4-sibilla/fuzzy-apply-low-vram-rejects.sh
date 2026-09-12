#!/usr/bin/env bash
set -euo pipefail

PORT="${PORT:-/home/sibilla-cumana/src/ds4-main-lowvram-port}"
SNAPSHOT="${SNAPSHOT:-/home/sibilla-cumana/ds4-sibilla-snapshot-20260912}"
OUT="${OUT:-$SNAPSHOT/upstream-porting/fuzzy-reject-pass}"
EXPECTED_HEAD="bd66c402070042bf0a79ad6ece8242de4c93680c"

if [[ ! -d "$PORT/.git" ]]; then
  echo "missing_port_clone: $PORT" >&2
  exit 1
fi

sudo -u sibilla-cumana -H mkdir -p "$OUT/logs" "$OUT/reject-backup"

sudo -u sibilla-cumana -H env PORT="$PORT" OUT="$OUT" EXPECTED_HEAD="$EXPECTED_HEAD" bash <<'INNER'
set -euo pipefail
cd "$PORT"

head="$(git rev-parse HEAD)"
if [[ "$head" != "$EXPECTED_HEAD" ]]; then
  echo "unexpected_head=$head expected=$EXPECTED_HEAD" >&2
  exit 1
fi

# Preserve the exact pre-pass state. This clone is disposable, but the backup
# makes every automatic fuzzy decision auditable/reversible.
git status --short --branch > "$OUT/status-before.txt"
git diff --binary > "$OUT/before-fuzzy.patch"

shopt -s nullglob
rejects=( *.rej tests/*.rej rocm/*.rej )
if ((${#rejects[@]} == 0)); then
  echo "no_rejects_found" >&2
  exit 0
fi

cp -a "${rejects[@]}" "$OUT/reject-backup/"
: > "$OUT/results.tsv"
: > "$OUT/resolved.txt"
: > "$OUT/unresolved.txt"

for rej in "${rejects[@]}"; do
  safe="${rej//\//__}"
  applied=0

  # git-apply is deliberately strict. GNU patch can often relocate the same
  # hunk safely after upstream moved nearby code. Only mutate after dry-run.
  for p in 1 0; do
    dry="$OUT/logs/${safe}.p${p}.dry.txt"
    apply="$OUT/logs/${safe}.p${p}.apply.txt"

    set +e
    patch --dry-run --batch --forward --fuzz=2 -p"$p" < "$rej" >"$dry" 2>&1
    rc=$?
    set -e

    if [[ $rc -eq 0 ]]; then
      patch --batch --forward --fuzz=2 -p"$p" < "$rej" >"$apply" 2>&1
      printf '%s\tresolved\tp%s\n' "$rej" "$p" >> "$OUT/results.tsv"
      printf '%s\n' "$rej" >> "$OUT/resolved.txt"
      applied=1
      break
    fi
  done

  if [[ $applied -eq 0 ]]; then
    printf '%s\tunresolved\t-\n' "$rej" >> "$OUT/results.tsv"
    printf '%s\n' "$rej" >> "$OUT/unresolved.txt"
  fi
done

# Do not delete .rej files: they remain evidence of the original git-apply
# failure. The results file records which ones have now been incorporated.
git diff --check > "$OUT/diff-check.txt" 2>&1 || true
git status --short --branch > "$OUT/status-after.txt"
git diff --stat > "$OUT/diff-stat-after.txt"
git diff --binary > "$OUT/after-fuzzy.patch"

grep -RInE \
  'cuda_low_vram_stream|cuda-low-vram-stream|DS4_CUDA_LOW_VRAM_(STAGE|RESERVE)_MB|ds4_gpu_set_cuda_low_vram_stream|low_vram_stage' \
  ds4.c ds4.h ds4_agent.c ds4_bench.c ds4_cli.c ds4_cuda.cu ds4_eval.c \
  ds4_gpu.h ds4_help.c ds4_server.c ds4_ssd.c ds4_ssd.h tests 2>/dev/null \
  > "$OUT/symbol-inventory-after.txt" || true

{
  echo "head=$head"
  echo "reject_files=${#rejects[@]}"
  echo "resolved=$(wc -l < "$OUT/resolved.txt")"
  echo "unresolved=$(wc -l < "$OUT/unresolved.txt")"
  echo "diff_lines=$(wc -l < "$OUT/after-fuzzy.patch")"
} > "$OUT/summary.txt"
INNER

echo '=== FUZZY REJECT PASS ==='
cat "$OUT/summary.txt"
echo
echo '=== RESULTS ==='
cat "$OUT/results.tsv"
echo
echo '=== DIFF CHECK ==='
cat "$OUT/diff-check.txt"
echo
echo '=== DIFF STAT ==='
cat "$OUT/diff-stat-after.txt"
echo
echo '=== UNRESOLVED ==='
cat "$OUT/unresolved.txt"
