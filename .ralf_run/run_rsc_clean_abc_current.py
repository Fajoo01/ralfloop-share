from __future__ import annotations

import subprocess
import sys
from pathlib import Path

RUN_DIR = Path(__file__).resolve().parent
HELPER = RUN_DIR / "rsc_abc_force_current_patch_correction.py"

def main() -> int:
    if not HELPER.exists():
        print(f"RSC_ERROR: helper missing: {HELPER}", file=sys.stderr)
        return 2

    print(f"RSC_FAST_WRAPPER: using {HELPER}")
    try:
        r = subprocess.run(
            [sys.executable, str(HELPER)],
            check=False,
            timeout=20,
        )
        return int(r.returncode or 0)
    except subprocess.TimeoutExpired:
        print("RSC_ERROR: helper timeout", file=sys.stderr)
        return 124

if __name__ == "__main__":
    raise SystemExit(main())
