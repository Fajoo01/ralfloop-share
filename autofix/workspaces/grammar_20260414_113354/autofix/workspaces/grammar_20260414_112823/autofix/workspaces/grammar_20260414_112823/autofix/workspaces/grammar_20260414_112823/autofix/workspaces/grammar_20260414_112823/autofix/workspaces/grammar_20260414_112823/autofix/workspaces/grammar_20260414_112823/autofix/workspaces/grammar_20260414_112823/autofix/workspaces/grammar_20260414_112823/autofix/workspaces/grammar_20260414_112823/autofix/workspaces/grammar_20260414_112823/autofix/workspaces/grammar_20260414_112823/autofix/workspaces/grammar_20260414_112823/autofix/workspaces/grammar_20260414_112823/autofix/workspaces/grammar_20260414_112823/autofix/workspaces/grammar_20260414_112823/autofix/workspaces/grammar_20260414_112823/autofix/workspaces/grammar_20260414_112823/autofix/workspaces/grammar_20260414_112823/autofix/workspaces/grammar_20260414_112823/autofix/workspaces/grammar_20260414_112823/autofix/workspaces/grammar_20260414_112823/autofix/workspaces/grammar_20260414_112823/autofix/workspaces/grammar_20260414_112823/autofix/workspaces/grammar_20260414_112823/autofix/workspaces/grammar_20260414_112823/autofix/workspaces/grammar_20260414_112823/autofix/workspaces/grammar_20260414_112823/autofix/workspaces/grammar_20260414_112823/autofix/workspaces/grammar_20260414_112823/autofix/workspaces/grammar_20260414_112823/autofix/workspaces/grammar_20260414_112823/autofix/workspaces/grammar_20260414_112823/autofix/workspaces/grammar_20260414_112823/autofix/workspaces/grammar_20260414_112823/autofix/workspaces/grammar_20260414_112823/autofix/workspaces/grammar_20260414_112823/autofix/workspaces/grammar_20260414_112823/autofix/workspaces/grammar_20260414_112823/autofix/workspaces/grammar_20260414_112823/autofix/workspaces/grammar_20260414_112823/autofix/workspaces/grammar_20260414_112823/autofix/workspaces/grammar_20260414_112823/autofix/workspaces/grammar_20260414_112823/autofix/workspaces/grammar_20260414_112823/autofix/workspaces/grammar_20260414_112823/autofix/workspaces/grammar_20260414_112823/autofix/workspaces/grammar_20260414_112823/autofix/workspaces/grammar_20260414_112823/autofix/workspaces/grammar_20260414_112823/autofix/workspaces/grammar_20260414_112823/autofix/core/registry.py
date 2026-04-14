from __future__ import annotations
import json
from pathlib import Path

BASE = Path("/home/sibilla-cumana/ralfloop_agent_scaffold")
TARGETS_DIR = BASE / "autofix" / "targets"

def load_targets():
    out = []
    for p in sorted(TARGETS_DIR.glob("*.json")):
        out.append(json.loads(p.read_text(encoding="utf-8")))
    return out
