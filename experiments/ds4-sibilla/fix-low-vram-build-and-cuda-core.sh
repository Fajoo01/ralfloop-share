#!/usr/bin/env bash
set -euo pipefail

PORT="${PORT:-/home/sibilla-cumana/src/ds4-main-lowvram-port}"
SNAPSHOT="${SNAPSHOT:-/home/sibilla-cumana/ds4-sibilla-snapshot-20260912}"
OUT="${OUT:-$SNAPSHOT/upstream-porting/build-fix-pass}"
EXPECTED_HEAD="bd66c402070042bf0a79ad6ece8242de4c93680c"
OWNER="${OWNER:-sibilla-cumana}"

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
sudo -u "$OWNER" -H sh -c 'git -C "$1" diff > "$2/before.patch"' sh "$PORT" "$OUT"
sudo -u "$OWNER" -H sh -c 'git -C "$1" status --short --branch > "$2/before-status.txt"' sh "$PORT" "$OUT"

sudo -u "$OWNER" -H env PORT="$PORT" OUT="$OUT" python3 - <<'PY'
from pathlib import Path
import os, re, sys

root = Path(os.environ['PORT'])
out = Path(os.environ['OUT'])
report = []

class PatchError(RuntimeError):
    pass

def load(name):
    p = root / name
    return p, p.read_text()

def save(p, s):
    p.write_text(s)

def function_span(src: str, signature: str):
    start = src.find(signature)
    if start < 0:
        raise PatchError(f"function signature not found: {signature}")
    brace = src.find('{', start)
    if brace < 0:
        raise PatchError(f"opening brace not found: {signature}")
    depth = 0
    i = brace
    while i < len(src):
        c = src[i]
        if c == '{':
            depth += 1
        elif c == '}':
            depth -= 1
            if depth == 0:
                return start, i + 1
        i += 1
    raise PatchError(f"unterminated function: {signature}")

# -------------------------------------------------------------------------
# ds4.c: the low-VRAM flag belongs to the generic CUDA/Metal graph wrapper,
# not to the GLM Metal-only leaf.  The partial reject application inserted it
# in the wrong signature, which shifted every following GLM argument.
# -------------------------------------------------------------------------
p, s = load('ds4.c')

sig_glm = 'static int generate_glm_metal_argmax('
a, b = function_span(s, sig_glm)
block = s[a:b]
wrong = '        bool                cuda_low_vram_stream,\n'
if wrong in block:
    if block.count(wrong) != 1:
        raise PatchError(f'ds4.c: GLM low-vram parameter count={block.count(wrong)}')
    block = block.replace(wrong, '', 1)
    s = s[:a] + block + s[b:]
    report.append('ds4.c: removed cuda_low_vram_stream from GLM Metal leaf')
else:
    report.append('ds4.c: GLM Metal leaf already clean')

# Recompute span after the previous edit.
sig_raw = 'static int generate_metal_graph_raw_swa('
a, b = function_span(s, sig_raw)
block = s[a:b]
needle = '        bool                ssd_streaming_cold,\n'
want = '        bool                cuda_low_vram_stream,\n'
header_end = block.find(') {')
if header_end < 0:
    raise PatchError('ds4.c: raw_swa signature terminator missing')
header = block[:header_end]
if 'cuda_low_vram_stream' not in header:
    if header.count(needle) != 1:
        raise PatchError(f'ds4.c: raw_swa cold parameter count={header.count(needle)}')
    block = block.replace(needle, needle + want, 1)
    s = s[:a] + block + s[b:]
    report.append('ds4.c: added cuda_low_vram_stream to raw_swa wrapper')
else:
    report.append('ds4.c: raw_swa wrapper already has cuda_low_vram_stream')

save(p, s)

# -------------------------------------------------------------------------
# ds4_cuda.cu: port the semantic content of all three rejected CUDA hunks.
# The upstream cache-limit function now defaults to UINT64_MAX.  In low-VRAM
# mode we need a stable default ceiling derived once from currently free VRAM,
# reserving both a guard and the fixed staging arena.  Explicit
# DS4_CUDA_WEIGHT_CACHE_LIMIT_GB continues to override this policy.
# -------------------------------------------------------------------------
p, s = load('ds4_cuda.cu')

sig_limit = 'static uint64_t cuda_model_cache_limit_bytes(void)'
a, b = function_span(s, sig_limit)
old_block = s[a:b]

if 'g_model_default_cache_limit_valid' not in s:
    new_block = r'''static uint64_t g_model_default_cache_limit;
static int g_model_default_cache_limit_valid;

static void cuda_model_cache_limit_reset(void) {
    g_model_default_cache_limit = 0;
    g_model_default_cache_limit_valid = 0;
}

static uint64_t cuda_model_cache_limit_bytes(void) {
    uint64_t gb = 0;
    const char *env = getenv("DS4_CUDA_WEIGHT_CACHE_LIMIT_GB");
    if (env && env[0]) {
        char *end = NULL;
        unsigned long long v = strtoull(env, &end, 10);
        if (end != env) gb = (uint64_t)v;
    }
    if (gb != 0) return gb * 1073741824ull;
    if (!g_cuda_low_vram_stream) return UINT64_MAX;
    if (g_model_default_cache_limit_valid) return g_model_default_cache_limit;

    size_t free_raw = 0;
    size_t total_raw = 0;
    const cudaError_t info_err = cudaMemGetInfo(&free_raw, &total_raw);
    if (info_err != cudaSuccess) {
        fprintf(stderr,
                "ds4: CUDA low-VRAM cudaMemGetInfo failed while planning "
                "persistent cache: %s; using zero persistent cache\n",
                cudaGetErrorString(info_err));
        (void)cudaGetLastError();
        g_model_default_cache_limit = 0;
        g_model_default_cache_limit_valid = 1;
        return 0;
    }

    const uint64_t free_now = (uint64_t)free_raw;
    const uint64_t reserve = cuda_low_vram_reserve_budget_bytes();
    const uint64_t stage = cuda_low_vram_stage_budget_bytes();
    const bool stage_preallocated = g_low_vram_stage_device != NULL;

    /* cudaMemGetInfo already excludes a preallocated staging arena. Recover
     * its bytes for planning, then subtract stage exactly once below. */
    uint64_t planning_free = free_now;
    if (stage_preallocated) {
        planning_free =
            free_now <= UINT64_MAX - stage ? free_now + stage : UINT64_MAX;
    }

    const uint64_t ceiling = (planning_free / 100ull) * 92ull;
    const uint64_t guard_pct = (planning_free / 100ull) * 8ull;
    const uint64_t guard = guard_pct > reserve ? guard_pct : reserve;
    const uint64_t guarded = ceiling > guard ? ceiling - guard : 0;

    g_model_default_cache_limit = guarded > stage ? guarded - stage : 0;
    g_model_default_cache_limit_valid = 1;

    if (getenv("DS4_CUDA_WEIGHT_CACHE_VERBOSE") || g_cuda_low_vram_stream) {
        fprintf(stderr,
                "ds4: CUDA low-VRAM cache plan: free=%.2f GiB "
                "planning-free=%.2f GiB ceiling=%.2f GiB "
                "guard=%.2f GiB stage=%.2f GiB preallocated=%d "
                "limit=%.2f GiB\n",
                (double)free_now / 1073741824.0,
                (double)planning_free / 1073741824.0,
                (double)ceiling / 1073741824.0,
                (double)guard / 1073741824.0,
                (double)stage / 1073741824.0,
                stage_preallocated ? 1 : 0,
                (double)g_model_default_cache_limit / 1073741824.0);
    }
    return g_model_default_cache_limit;
}'''
    s = s[:a] + new_block + s[b:]
    report.append('ds4_cuda.cu: ported stable low-VRAM persistent-cache ceiling')
else:
    report.append('ds4_cuda.cu: persistent-cache ceiling already ported')

# Add the two missing staging fallbacks inside cuda_model_range_ptr_from_fd.
a, b = function_span(s, 'static const char *cuda_model_range_ptr_from_fd(')
block = s[a:b]

budget_old = '''    if (g_model_range_bytes > limit || bytes > limit - g_model_range_bytes) {
        if (getenv("DS4_CUDA_WEIGHT_CACHE_VERBOSE")) {
            fprintf(stderr, "ds4: CUDA direct %s %.2f MiB (cache budget %.2f GiB exhausted)\\n",
                    what ? what : "weights",
                    (double)bytes / 1048576.0,
                    (double)limit / 1073741824.0);
        }
        return cuda_model_ptr(model_map, offset);
    }
'''
budget_new = '''    if (g_model_range_bytes > limit || bytes > limit - g_model_range_bytes) {
        if (getenv("DS4_CUDA_WEIGHT_CACHE_VERBOSE")) {
            fprintf(stderr, "ds4: CUDA direct %s %.2f MiB (cache budget %.2f GiB exhausted)\\n",
                    what ? what : "weights",
                    (double)bytes / 1048576.0,
                    (double)limit / 1073741824.0);
        }
        if (g_cuda_low_vram_stream) {
            return cuda_low_vram_stage_range(model_map, offset, bytes, what);
        }
        return cuda_model_ptr(model_map, offset);
    }
'''
if 'cache budget %.2f GiB exhausted' not in block:
    raise PatchError('ds4_cuda.cu: cache-budget fallback anchor missing')
if 'cache budget %.2f GiB exhausted' in block and budget_old in block:
    block = block.replace(budget_old, budget_new, 1)
    report.append('ds4_cuda.cu: staged fallback on persistent-cache budget exhaustion')
elif budget_new in block:
    report.append('ds4_cuda.cu: cache-budget staged fallback already present')
else:
    raise PatchError('ds4_cuda.cu: cache-budget block changed unexpectedly')

alloc_old = '''    char *dev = cuda_model_arena_alloc(bytes, what);
    if (!dev) {
        if (getenv("DS4_CUDA_STRICT_WEIGHT_CACHE") != NULL) return NULL;
        return cuda_model_ptr(model_map, offset);
    }
'''
alloc_new = '''    char *dev = cuda_model_arena_alloc(bytes, what);
    if (!dev) {
        if (g_cuda_low_vram_stream) {
            return cuda_low_vram_stage_range(model_map, offset, bytes, what);
        }
        if (getenv("DS4_CUDA_STRICT_WEIGHT_CACHE") != NULL) return NULL;
        return cuda_model_ptr(model_map, offset);
    }
'''
if alloc_old in block:
    block = block.replace(alloc_old, alloc_new, 1)
    report.append('ds4_cuda.cu: staged fallback on persistent arena allocation refusal')
elif alloc_new in block:
    report.append('ds4_cuda.cu: arena staged fallback already present')
else:
    raise PatchError('ds4_cuda.cu: arena allocation fallback anchor missing')

s = s[:a] + block + s[b:]
save(p, s)

(out/'edit-report.txt').write_text('\n'.join(report) + '\n')
print('\n'.join(report))
PY

sudo -u "$OWNER" -H sh -c 'git -C "$1" diff --check > "$2/diff-check.txt" 2>&1' sh "$PORT" "$OUT" || true
sudo -u "$OWNER" -H sh -c 'git -C "$1" diff --stat > "$2/after-stat.txt"' sh "$PORT" "$OUT"
sudo -u "$OWNER" -H sh -c 'git -C "$1" diff > "$2/after.patch"' sh "$PORT" "$OUT"

set +e
sudo -u "$OWNER" -H make -C "$PORT" -j2 ds4 ds4-server >"$OUT/build.stdout" 2>"$OUT/build.stderr"
BUILD_RC=$?
set -e
printf '%s\n' "$BUILD_RC" | sudo -u "$OWNER" -H tee "$OUT/build.rc" >/dev/null

set +e
sudo -u "$OWNER" -H "$PORT/ds4" --help >"$OUT/help.stdout" 2>"$OUT/help.stderr"
HELP_RC=$?
sudo -u "$OWNER" -H "$PORT/ds4" --cuda-low-vram-stream -m /dev/null >"$OUT/prereq.stdout" 2>"$OUT/prereq.stderr"
PREREQ_RC=$?
set -e
printf '%s\n' "$HELP_RC" | sudo -u "$OWNER" -H tee "$OUT/help.rc" >/dev/null
printf '%s\n' "$PREREQ_RC" | sudo -u "$OWNER" -H tee "$OUT/prereq.rc" >/dev/null

sudo -u "$OWNER" -H sh -c '
  cd "$1"
  {
    echo "=== LOW-VRAM CORE SYMBOLS ==="
    grep -nE "g_model_default_cache_limit|cuda_model_cache_limit_reset|CUDA low-VRAM cache plan|cuda_low_vram_stage_range|generate_metal_graph_raw_swa|generate_glm_metal_argmax" ds4_cuda.cu ds4.c 2>/dev/null || true
    echo
    echo "=== REMAINING CUDA REJECT HEADERS (historical .rej file) ==="
    grep -n "^@@" ds4_cuda.cu.rej 2>/dev/null || true
  } > "$2/symbols.txt"
' sh "$PORT" "$OUT"

echo '=== BUILD/CUDA CORE FIX REPORT ==='
cat "$OUT/edit-report.txt"
echo
echo '=== DIFF CHECK ==='
cat "$OUT/diff-check.txt"
echo
echo '=== DIFF STAT ==='
cat "$OUT/after-stat.txt"
echo
echo '=== BUILD RC ==='
cat "$OUT/build.rc"
echo
echo '=== BUILD STDERR TAIL ==='
tail -n 120 "$OUT/build.stderr" || true
echo
echo '=== HELP / PREREQ RC ==='
printf 'help_rc='; cat "$OUT/help.rc"
printf 'prereq_rc='; cat "$OUT/prereq.rc"
echo '--- prerequisite stderr ---'
cat "$OUT/prereq.stderr" || true
echo
echo '=== SYMBOLS ==='
sed -n '1,260p' "$OUT/symbols.txt"
echo
echo '=== OUTPUT ==='
ls -lh "$OUT"
