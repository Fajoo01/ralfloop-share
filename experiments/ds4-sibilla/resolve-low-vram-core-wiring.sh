#!/usr/bin/env bash
set -euo pipefail

PORT="${PORT:-/home/sibilla-cumana/src/ds4-main-lowvram-port}"
SNAPSHOT="${SNAPSHOT:-/home/sibilla-cumana/ds4-sibilla-snapshot-20260912}"
OUT="${OUT:-$SNAPSHOT/upstream-porting/core-wiring-pass}"
EXPECTED_HEAD="bd66c402070042bf0a79ad6ece8242de4c93680c"
OWNER="${OWNER:-sibilla-cumana}"

if [[ ! -d "$PORT/.git" ]]; then
  echo "missing_port_clone: $PORT" >&2
  exit 1
fi

# The port clone belongs to sibilla-cumana. Always run Git as the repository
# owner instead of weakening Git ownership checks with safe.directory='*'.
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
import re

root = Path(__import__('os').environ['PORT'])
out = Path(__import__('os').environ['OUT'])
report = []

class PatchError(RuntimeError):
    pass

def load(name):
    p = root / name
    return p, p.read_text()

def save(p, text):
    p.write_text(text)

def insert_after_unique(name, anchor, addition, marker):
    p, s = load(name)
    if marker in s:
        report.append(f"{name}: already:{marker}")
        return
    n = s.count(anchor)
    if n != 1:
        raise PatchError(f"{name}: anchor_count={n}: {anchor!r}")
    s = s.replace(anchor, anchor + addition, 1)
    save(p, s)
    report.append(f"{name}: inserted:{marker}")

def insert_before_unique(name, anchor, addition, marker):
    p, s = load(name)
    if marker in s:
        report.append(f"{name}: already:{marker}")
        return
    n = s.count(anchor)
    if n != 1:
        raise PatchError(f"{name}: anchor_count={n}: {anchor!r}")
    s = s.replace(anchor, addition + anchor, 1)
    save(p, s)
    report.append(f"{name}: inserted:{marker}")

# 1) Public CUDA control declaration.
insert_after_unique(
    'ds4_gpu.h',
    'void ds4_gpu_set_ssd_streaming(bool enabled);\n',
    'void ds4_gpu_set_cuda_low_vram_stream(bool enabled);\n',
    'ds4_gpu_set_cuda_low_vram_stream(bool enabled);')

# 2) Help wiring.
p, s = load('ds4_help.c')
if '"--cuda-low-vram-stream"' not in s:
    m = re.search(r'(?m)^(\s*)opt\(fp, c, "--ssd-streaming-cold".*\n', s)
    if not m:
        raise PatchError('ds4_help.c: ssd-streaming-cold help anchor missing')
    indent = m.group(1)
    add = indent + 'opt(fp, c, "--cuda-low-vram-stream", "Experimental CUDA SSD mode: stage dense weights through a bounded device buffer.");\n'
    s = s[:m.start()] + add + s[m.start():]
    save(p, s)
    report.append('ds4_help.c: inserted:--cuda-low-vram-stream')
else:
    report.append('ds4_help.c: already:--cuda-low-vram-stream')

# 3) Bench engine option propagation.
insert_before_unique(
    'ds4_bench.c',
    '        .ssd_streaming_cold = cfg.ssd_streaming_cold,\n',
    '        .cuda_low_vram_stream = cfg.cuda_low_vram_stream,\n',
    '.cuda_low_vram_stream = cfg.cuda_low_vram_stream,')

# 4) ds4_engine private field. Restrict insertion to struct ds4_engine only.
p, s = load('ds4.c')
engine_start = s.find('struct ds4_engine {')
if engine_start < 0:
    raise PatchError('ds4.c: struct ds4_engine not found')
engine_end = s.find('\n};', engine_start)
if engine_end < 0:
    raise PatchError('ds4.c: struct ds4_engine terminator not found')
block = s[engine_start:engine_end]
if 'cuda_low_vram_stream' not in block:
    needle = '    bool ssd_streaming_cold;\n'
    if block.count(needle) != 1:
        raise PatchError(f'ds4.c: engine ssd_streaming_cold count={block.count(needle)}')
    block2 = block.replace(needle, '    bool cuda_low_vram_stream;\n' + needle, 1)
    s = s[:engine_start] + block2 + s[engine_end:]
    save(p, s)
    report.append('ds4.c: inserted:engine.cuda_low_vram_stream')
else:
    report.append('ds4.c: already:engine.cuda_low_vram_stream')

# 5) Validate the mode at engine creation.
p, s = load('ds4.c')
err_marker = '--cuda-low-vram-stream requires --cuda and --ssd-streaming'
if err_marker not in s:
    anchor = '    e->cuda_low_vram_stream = opt->cuda_low_vram_stream;\n'
    if s.count(anchor) != 1:
        raise PatchError(f'ds4.c: option assignment count={s.count(anchor)}')
    validation = '''    if (opt->cuda_low_vram_stream &&\n        (opt->backend != DS4_BACKEND_CUDA || !opt->ssd_streaming)) {\n        fprintf(stderr,\n                "ds4: --cuda-low-vram-stream requires --cuda and --ssd-streaming\\n");\n        free(e);\n        *out = NULL;\n        return 1;\n    }\n'''
    s = s.replace(anchor, anchor + validation, 1)
    save(p, s)
    report.append('ds4.c: inserted:low-vram validation')
else:
    report.append('ds4.c: already:low-vram validation')

# 6) Drive the CUDA backend global mode next to SSD streaming setup.
p, s = load('ds4.c')
if 'ds4_gpu_set_cuda_low_vram_stream(e->cuda_low_vram_stream);' not in s:
    candidates = [m for m in re.finditer(r'(?m)^(\s*)ds4_gpu_set_ssd_streaming\(e->ssd_streaming\);\s*$', s)]
    if len(candidates) != 1:
        raise PatchError(f'ds4.c: gpu ssd setter candidates={len(candidates)}')
    m = candidates[0]
    line_end = s.find('\n', m.end())
    if line_end < 0:
        line_end = m.end()
    addition = '\n' + m.group(1) + 'ds4_gpu_set_cuda_low_vram_stream(e->cuda_low_vram_stream);'
    s = s[:line_end] + addition + s[line_end:]
    save(p, s)
    report.append('ds4.c: inserted:GPU low-vram setter')
else:
    report.append('ds4.c: already:GPU low-vram setter')

# 7) Session graph propagation.
p, s = load('ds4.c')
if 's->graph.cuda_low_vram_stream = e->cuda_low_vram_stream;' not in s:
    anchor = '    s->graph.ssd_streaming_cold = e->ssd_streaming_cold;\n'
    n = s.count(anchor)
    if n != 1:
        raise PatchError(f'ds4.c: session graph cold assignment count={n}')
    s = s.replace(anchor,
                  '    s->graph.cuda_low_vram_stream = e->cuda_low_vram_stream;\n' + anchor,
                  1)
    save(p, s)
    report.append('ds4.c: inserted:session graph low-vram propagation')
else:
    report.append('ds4.c: already:session graph low-vram propagation')

# 8) CLI help test assertion.
p, s = load('tests/test_gpu_args_cli.sh')
marker = '--help mentions --cuda-low-vram-stream'
if marker not in s:
    lines = s.splitlines(True)
    idx = None
    for i, line in enumerate(lines):
        if '--help mentions --ssd-streaming-cold' in line:
            idx = i + 1
            break
    if idx is None:
        for i, line in enumerate(lines):
            if 'assert_grep' in line and '--help' in ''.join(lines[max(0, i-8):i+1]):
                idx = i + 1
                break
    if idx is None:
        report.append('tests/test_gpu_args_cli.sh: skipped:no safe help anchor')
    else:
        lines.insert(idx, '    assert_grep "$name --help mentions --cuda-low-vram-stream" "cuda-low-vram-stream" "$LOG"\n')
        save(p, ''.join(lines))
        report.append('tests/test_gpu_args_cli.sh: inserted:help assertion')
else:
    report.append('tests/test_gpu_args_cli.sh: already:help assertion')

# Intentionally defer ds4_ssd.{c,h} helpers and the three CUDA reject hunks.
report.append('ds4_ssd.c/h: deferred:CUDA file already contains stage/reserve parsers')
report.append('README.md: deferred:documentation after runtime compiles')
report.append('ds4_cuda.cu: 3 rejected hunks deferred for dedicated semantic port')

(out / 'edit-report.txt').write_text('\n'.join(report) + '\n')
print('\n'.join(report))
PY

sudo -u "$OWNER" -H sh -c 'git -C "$1" diff --check > "$2/diff-check.txt" 2>&1' sh "$PORT" "$OUT" || true
sudo -u "$OWNER" -H sh -c 'git -C "$1" diff --stat > "$2/after-stat.txt"' sh "$PORT" "$OUT"
sudo -u "$OWNER" -H sh -c 'git -C "$1" diff > "$2/after.patch"' sh "$PORT" "$OUT"

# Build only in the isolated clone. Keep shell redirections inside the OWNER
# shell too, otherwise bandi may be unable to create files under OUT.
set +e
sudo -u "$OWNER" -H sh -c 'make -C "$1" -j2 ds4 ds4-server > "$2/build.stdout" 2> "$2/build.stderr"' sh "$PORT" "$OUT"
BUILD_RC=$?
set -e
printf '%s\n' "$BUILD_RC" | sudo -u "$OWNER" -H tee "$OUT/build.rc" >/dev/null

sudo -u "$OWNER" -H sh -c '
  cd "$1"
  {
    echo "=== CORE SYMBOLS ==="
    grep -nE "cuda_low_vram_stream|ds4_gpu_set_cuda_low_vram_stream|cuda-low-vram-stream" ds4.c ds4.h ds4_gpu.h ds4_help.c ds4_bench.c tests/test_gpu_args_cli.sh 2>/dev/null || true
    echo
    echo "=== CUDA DEFERRED REJECT HEADERS ==="
    grep -n "^@@" ds4_cuda.cu.rej 2>/dev/null || true
  } > "$2/symbols.txt"
' sh "$PORT" "$OUT"

echo '=== CORE WIRING EDIT REPORT ==='
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
tail -n 100 "$OUT/build.stderr" || true
echo
echo '=== CORE SYMBOLS / CUDA REJECTS ==='
sed -n '1,240p' "$OUT/symbols.txt"
echo
echo '=== OUTPUT ==='
ls -lh "$OUT"
