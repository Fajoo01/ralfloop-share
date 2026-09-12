#!/usr/bin/env bash
set -euo pipefail

PORT_DIR="${PORT_DIR:-/home/sibilla-cumana/src/ds4-main-lowvram-port}"
OWNER="${OWNER:-sibilla-cumana}"

if [[ ! -d "$PORT_DIR" ]]; then
  echo "missing_port_dir: $PORT_DIR" >&2
  exit 1
fi

echo '=== EXACT MESSAGE MATCHES ==='
sudo -u "$OWNER" -H grep -RniF \
  'another ds4 process is already running' \
  "$PORT_DIR" \
  --exclude='*.o' --exclude='ds4' --exclude='ds4-server' --exclude='ds4-bench' --exclude='ds4-eval' --exclude='ds4-agent' \
  2>/dev/null || true

echo
echo '=== REFUSING / SINGLE INSTANCE / PID / LOCK CANDIDATES ==='
sudo -u "$OWNER" -H grep -RniE \
  'refusing to start|single.?instance|pidfile|pid_file|lockfile|lock_file|flock\(|F_SETLK|F_SETLKW|kill\([^,]+,[[:space:]]*0\)|/proc/[0-9]|DS4_.*(LOCK|PID|INSTANCE)|getpid\(' \
  "$PORT_DIR" \
  --include='*.c' --include='*.h' --include='*.cu' --include='*.sh' \
  2>/dev/null | head -n 240 || true

echo
echo '=== SOURCE CONTEXT AROUND EXACT MESSAGE ==='
sudo -u "$OWNER" -H python3 - "$PORT_DIR" <<'PY'
from pathlib import Path
import sys
root = Path(sys.argv[1])
needle = 'another ds4 process is already running'
seen = 0
for p in root.rglob('*'):
    if not p.is_file() or p.suffix not in {'.c','.h','.cu','.sh','.cc','.cpp'}:
        continue
    try:
        text = p.read_text(errors='replace')
    except Exception:
        continue
    if needle not in text:
        continue
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if needle not in line:
            continue
        seen += 1
        lo = max(0, i-35)
        hi = min(len(lines), i+36)
        print(f'--- {p.relative_to(root)}:{i+1} ---')
        for j in range(lo, hi):
            print(f'{j+1:7d}: {lines[j]}')
if not seen:
    print('no_source_match')
PY

echo
echo '=== BINARY STRINGS NEAR MESSAGE ==='
if [[ -x "$PORT_DIR/ds4-server" ]]; then
  sudo -u "$OWNER" -H sh -c 'strings "$1/ds4-server" | grep -E -C 8 "another ds4 process|refusing to start|DS4_.*(LOCK|PID|INSTANCE)"' sh "$PORT_DIR" || true
fi
