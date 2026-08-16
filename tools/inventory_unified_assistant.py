#!/usr/bin/env python3
from __future__ import annotations

import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ralfloop_agent.unified_assistant.inventory import architecture_census


def main() -> int:
    print(json.dumps(architecture_census(), ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
