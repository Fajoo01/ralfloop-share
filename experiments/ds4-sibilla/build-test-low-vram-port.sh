#!/usr/bin/env bash
set -euo pipefail

PORT="${PORT:-/home/sibilla-cumana/src/ds4-main-lowvram-port}"
SNAPSHOT="${SNAPSHOT:-/home/sibilla-cumana/ds4-sibilla-snapshot-20260912}"
OUT="${OUT:-$SNAPSHOT/upstream-porting/build-test-pass}"
OWNER="${OWNER:-sibilla-cumana}"
EXPECTED_HEAD="bd66c402070042bf0a79ad6ece8242de4c93680c"
CUDA_ARCH="${CUDA_ARCH:-sm_75}"

if [[ ! -d "$PORT/.git" ]]; then
  echo "missing_port_clone: $PORT" >&2
  exit 1
fi

HEAD="$(sudo -u "$OWNER" -H git -C "$PORT" rev-parse HEAD)"
if [[ "$HEAD" != "$EXPECTED_HEAD" ]]; then
  echo "unexpected_head: $HEAD" >&2
  echo "expected_head:   $EXPECTED_HEAD" >&2
  exit 1
fi

sudo -u "$OWNER" -H mkdir -p "$OUT"

sudo -u "$OWNER" -H sh -c 'git -C "$1" diff --check > "$2/diff-check.txt" 2>&1' sh "$PORT" "$OUT" || true
sudo -u "$OWNER" -H sh -c 'git -C "$1" diff --stat > "$2/diff-stat.txt"' sh "$PORT" "$OUT"
sudo -u "$OWNER" -H sh -c 'git -C "$1" status --short --branch > "$2/status.txt"' sh "$PORT" "$OUT"

sudo -u "$OWNER" -H sh -c '
  {
    echo "CUDA_ARCH=$3"
    printf "nvcc_path="
    command -v nvcc || true
    nvcc --version || true
  } > "$2/toolchain.txt" 2>&1
' sh "$PORT" "$OUT" "$CUDA_ARCH"

set +e
sudo -u "$OWNER" -H sh -c 'make -C "$1" -B -j2 ds4 ds4-server CUDA_ARCH="$3" > "$2/build.stdout" 2> "$2/build.stderr"' sh "$PORT" "$OUT" "$CUDA_ARCH"
BUILD_RC=$?
set -e
printf '%s\n' "$BUILD_RC" | sudo -u "$OWNER" -H tee "$OUT/build.rc" >/dev/null

HELP_RC=127
PREREQ_RC=127
if [[ "$BUILD_RC" -eq 0 ]]; then
  set +e
  sudo -u "$OWNER" -H sh -c '"$1/ds4" --help > "$2/help.stdout" 2> "$2/help.stderr"' sh "$PORT" "$OUT"
  HELP_RC=$?
  sudo -u "$OWNER" -H sh -c '"$1/ds4" --cuda-low-vram-stream -m /dev/null > "$2/prereq.stdout" 2> "$2/prereq.stderr"' sh "$PORT" "$OUT"
  PREREQ_RC=$?
  set -e
fi
printf '%s\n' "$HELP_RC" | sudo -u "$OWNER" -H tee "$OUT/help.rc" >/dev/null
printf '%s\n' "$PREREQ_RC" | sudo -u "$OWNER" -H tee "$OUT/prereq.rc" >/dev/null

sudo -u "$OWNER" -H sh -c '
  cd "$1"
  {
    echo "=== LOW-VRAM CORE SYMBOLS ==="
    grep -nE "g_model_default_cache_limit|cuda_model_cache_limit_reset|CUDA low-VRAM cache plan|cuda_low_vram_stage_range|generate_metal_graph_raw_swa|generate_glm_metal_argmax" ds4_cuda.cu ds4.c 2>/dev/null || true
  } > "$2/symbols.txt"
' sh "$PORT" "$OUT"

echo '=== TOOLCHAIN ==='
cat "$OUT/toolchain.txt"
echo
echo '=== STATUS ==='
cat "$OUT/status.txt"
echo
echo '=== DIFF CHECK ==='
cat "$OUT/diff-check.txt"
echo
echo '=== DIFF STAT ==='
cat "$OUT/diff-stat.txt"
echo
echo '=== BUILD RC ==='
cat "$OUT/build.rc"
echo
echo '=== BUILD STDERR TAIL ==='
tail -n 180 "$OUT/build.stderr" 2>/dev/null || true
echo
echo '=== BUILD STDOUT TAIL ==='
tail -n 80 "$OUT/build.stdout" 2>/dev/null || true
echo
echo '=== HELP / PREREQ RC ==='
printf 'help_rc='; cat "$OUT/help.rc"
printf 'prereq_rc='; cat "$OUT/prereq.rc"
echo '--- prerequisite stderr ---'
cat "$OUT/prereq.stderr" 2>/dev/null || true
echo
echo '=== SYMBOLS ==='
sed -n '1,260p' "$OUT/symbols.txt"
echo
echo '=== OUTPUT ==='
ls -lh "$OUT"

exit "$BUILD_RC"
