#!/usr/bin/env bash
set -euo pipefail

UPSTREAM_SHA="${UPSTREAM_SHA:-bd66c402070042bf0a79ad6ece8242de4c93680c}"
PORT_DIR="${PORT_DIR:-/home/sibilla-cumana/src/ds4-main-lowvram-port}"
SNAPSHOT="${SNAPSHOT:-/home/sibilla-cumana/ds4-sibilla-snapshot-20260912}"
PATCH="${PATCH:-$SNAPSHOT/upstream-porting/local-feature-delta.patch}"
OUT="${OUT:-$SNAPSHOT/upstream-porting/port-attempt}"

sudo -u sibilla-cumana -H env \
  UPSTREAM_SHA="$UPSTREAM_SHA" PORT_DIR="$PORT_DIR" PATCH="$PATCH" OUT="$OUT" \
  bash -c '
set -euo pipefail

if [[ ! -s "$PATCH" ]]; then
  echo "feature_patch_missing_or_empty: $PATCH" >&2
  exit 2
fi

mkdir -p "$OUT"

if [[ -e "$PORT_DIR" ]]; then
  if [[ ! -d "$PORT_DIR/.git" ]]; then
    echo "port_dir_exists_and_is_not_git_repo: $PORT_DIR" >&2
    exit 2
  fi
  if [[ -n "$(git -C "$PORT_DIR" status --porcelain 2>/dev/null || true)" ]]; then
    echo "port_dir_is_dirty_refusing_to_modify: $PORT_DIR" >&2
    git -C "$PORT_DIR" status --short --branch >&2 || true
    exit 2
  fi
else
  git clone --filter=blob:none https://github.com/antirez/ds4.git "$PORT_DIR"
fi

git -C "$PORT_DIR" fetch origin main

git -C "$PORT_DIR" checkout --detach "$UPSTREAM_SHA"

{
  echo "upstream_sha=$UPSTREAM_SHA"
  echo "patch=$PATCH"
  echo "port_dir=$PORT_DIR"
  git -C "$PORT_DIR" log -1 --oneline --decorate
} > "$OUT/baseline.txt"

set +e
git -C "$PORT_DIR" apply --check "$PATCH" >"$OUT/apply-check.stdout" 2>"$OUT/apply-check.stderr"
plain_rc=$?
git -C "$PORT_DIR" apply --3way --check "$PATCH" >"$OUT/apply-3way-check.stdout" 2>"$OUT/apply-3way-check.stderr"
threeway_rc=$?
set -e

printf "%s\n" "$plain_rc" > "$OUT/apply-check.rc"
printf "%s\n" "$threeway_rc" > "$OUT/apply-3way-check.rc"

# If the patch applies cleanly, apply it in the isolated checkout. Otherwise do a
# reject-producing attempt there so we can see exactly which hunks need a manual port.
if [[ "$plain_rc" -eq 0 ]]; then
  git -C "$PORT_DIR" apply "$PATCH"
  echo clean > "$OUT/mode.txt"
else
  set +e
  git -C "$PORT_DIR" apply --reject --whitespace=nowarn "$PATCH" \
    >"$OUT/apply-reject.stdout" 2>"$OUT/apply-reject.stderr"
  reject_rc=$?
  set -e
  printf "%s\n" "$reject_rc" > "$OUT/apply-reject.rc"
  echo rejects > "$OUT/mode.txt"
fi

git -C "$PORT_DIR" status --short --branch > "$OUT/status.txt"
git -C "$PORT_DIR" diff --stat > "$OUT/diff-stat.txt"
git -C "$PORT_DIR" diff > "$OUT/applied-delta.patch"
find "$PORT_DIR" -name "*.rej" -type f -print | sort > "$OUT/reject-files.txt"

: > "$OUT/reject-summary.txt"
while IFS= read -r rej; do
  [[ -n "$rej" ]] || continue
  {
    echo "===== ${rej#$PORT_DIR/} ====="
    sed -n "1,220p" "$rej"
    echo
  } >> "$OUT/reject-summary.txt"
done < "$OUT/reject-files.txt"

# Surface where the custom capability landed (or failed to land).
grep -RInE \
  "cuda_low_vram_stream|cuda-low-vram-stream|DS4_CUDA_LOW_VRAM_STAGE_MB|DS4_CUDA_LOW_VRAM_RESERVE_MB" \
  "$PORT_DIR" \
  --exclude-dir=.git --exclude="*.rej" \
  > "$OUT/ported-symbol-hits.txt" || true

printf "plain_apply_check_rc=%s\nthreeway_apply_check_rc=%s\nmode=%s\n" \
  "$plain_rc" "$threeway_rc" "$(cat "$OUT/mode.txt")"

echo
echo "=== STATUS ==="
cat "$OUT/status.txt"
echo
echo "=== DIFF STAT ==="
cat "$OUT/diff-stat.txt"
echo
echo "=== REJECT FILES ==="
cat "$OUT/reject-files.txt"
echo
echo "=== PORTED SYMBOL HITS ==="
sed -n "1,220p" "$OUT/ported-symbol-hits.txt"
echo
echo "=== OUTPUT ==="
ls -lh "$OUT"
'
