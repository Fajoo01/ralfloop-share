#!/usr/bin/env bash
set -euo pipefail

MODERN="${MODERN:-/home/sibilla-cumana/src/ds4-main-lowvram-port}"
PROD="${PROD:-/home/sibilla-cumana/src/ds4-cuda-stream-pr739}"
OWNER="${OWNER:-sibilla-cumana}"

for d in "$MODERN" "$PROD"; do
  [[ -f "$d/ds4.c" ]] || { echo "missing $d/ds4.c" >&2; exit 1; }
done

show_anchor() {
  local label="$1" file="$2" pattern="$3" before="$4" after="$5"
  echo "=== $label ==="
  sudo -u "$OWNER" -H python3 - "$file" "$pattern" "$before" "$after" <<'PY'
from pathlib import Path
import re, sys
p = Path(sys.argv[1])
pat = re.compile(sys.argv[2])
before = int(sys.argv[3]); after = int(sys.argv[4])
lines = p.read_text(errors='replace').splitlines()
hits = [i for i,s in enumerate(lines) if pat.search(s)]
print(f"file={p} hits={len(hits)}")
for n,i in enumerate(hits,1):
    lo=max(0,i-before); hi=min(len(lines),i+after+1)
    print(f"--- hit {n}/{len(hits)} line={i+1} ---")
    for j in range(lo,hi):
        print(f"{j+1:6d} {lines[j]}")
PY
  echo
}

show_anchor "MODERN resident-model print/formula" "$MODERN/ds4.c" 'resident model' 70 45
show_anchor "PRODUCTION resident-model print/formula" "$PROD/ds4.c" 'resident model' 70 45

show_anchor "MODERN startup_model_span_bytes assignments" "$MODERN/ds4.c" 'startup_model_span_bytes\s*=' 28 35
show_anchor "PRODUCTION startup_model_span_bytes assignments" "$PROD/ds4.c" 'startup_model_span_bytes\s*=' 28 35

show_anchor "MODERN streaming initial-map decision" "$MODERN/ds4.c" 'initial cuda model map|initial .* model map|span_bytes\s*=' 18 30
show_anchor "PRODUCTION streaming initial-map decision" "$PROD/ds4.c" 'initial cuda model map|initial .* model map|span_bytes\s*=' 18 30

echo '=== SAFETY STATE ==='
grep -E '^(MemAvailable|Dirty|Writeback):' /proc/meminfo
printf 'production_19194='; ss -ltn | grep -q ':19194 ' && echo yes || echo no
printf 'test_19195='; ss -ltn | grep -q ':19195 ' && echo yes || echo no

echo
echo 'FORMULA_INSPECT_OK: static source inspection only; no runtime started or stopped; production untouched'
