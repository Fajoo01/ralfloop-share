#!/usr/bin/env python3
from __future__ import annotations

import os
from pathlib import Path

ENTRYPOINT = Path(os.getenv("RALF_MOBILE_USE_MCP_COMMAND", "/home/bandi/.local/share/ralf-mobile-use-mcp/.venv/bin/mobile-use-mcp"))

def main() -> int:
    if not ENTRYPOINT.is_absolute() or not ENTRYPOINT.is_file() or not os.access(ENTRYPOINT, os.X_OK):
        raise SystemExit("mobile_use_entrypoint_unavailable")
    os.execv(str(ENTRYPOINT), [str(ENTRYPOINT)])
    return 127

if __name__ == "__main__":
    raise SystemExit(main())
