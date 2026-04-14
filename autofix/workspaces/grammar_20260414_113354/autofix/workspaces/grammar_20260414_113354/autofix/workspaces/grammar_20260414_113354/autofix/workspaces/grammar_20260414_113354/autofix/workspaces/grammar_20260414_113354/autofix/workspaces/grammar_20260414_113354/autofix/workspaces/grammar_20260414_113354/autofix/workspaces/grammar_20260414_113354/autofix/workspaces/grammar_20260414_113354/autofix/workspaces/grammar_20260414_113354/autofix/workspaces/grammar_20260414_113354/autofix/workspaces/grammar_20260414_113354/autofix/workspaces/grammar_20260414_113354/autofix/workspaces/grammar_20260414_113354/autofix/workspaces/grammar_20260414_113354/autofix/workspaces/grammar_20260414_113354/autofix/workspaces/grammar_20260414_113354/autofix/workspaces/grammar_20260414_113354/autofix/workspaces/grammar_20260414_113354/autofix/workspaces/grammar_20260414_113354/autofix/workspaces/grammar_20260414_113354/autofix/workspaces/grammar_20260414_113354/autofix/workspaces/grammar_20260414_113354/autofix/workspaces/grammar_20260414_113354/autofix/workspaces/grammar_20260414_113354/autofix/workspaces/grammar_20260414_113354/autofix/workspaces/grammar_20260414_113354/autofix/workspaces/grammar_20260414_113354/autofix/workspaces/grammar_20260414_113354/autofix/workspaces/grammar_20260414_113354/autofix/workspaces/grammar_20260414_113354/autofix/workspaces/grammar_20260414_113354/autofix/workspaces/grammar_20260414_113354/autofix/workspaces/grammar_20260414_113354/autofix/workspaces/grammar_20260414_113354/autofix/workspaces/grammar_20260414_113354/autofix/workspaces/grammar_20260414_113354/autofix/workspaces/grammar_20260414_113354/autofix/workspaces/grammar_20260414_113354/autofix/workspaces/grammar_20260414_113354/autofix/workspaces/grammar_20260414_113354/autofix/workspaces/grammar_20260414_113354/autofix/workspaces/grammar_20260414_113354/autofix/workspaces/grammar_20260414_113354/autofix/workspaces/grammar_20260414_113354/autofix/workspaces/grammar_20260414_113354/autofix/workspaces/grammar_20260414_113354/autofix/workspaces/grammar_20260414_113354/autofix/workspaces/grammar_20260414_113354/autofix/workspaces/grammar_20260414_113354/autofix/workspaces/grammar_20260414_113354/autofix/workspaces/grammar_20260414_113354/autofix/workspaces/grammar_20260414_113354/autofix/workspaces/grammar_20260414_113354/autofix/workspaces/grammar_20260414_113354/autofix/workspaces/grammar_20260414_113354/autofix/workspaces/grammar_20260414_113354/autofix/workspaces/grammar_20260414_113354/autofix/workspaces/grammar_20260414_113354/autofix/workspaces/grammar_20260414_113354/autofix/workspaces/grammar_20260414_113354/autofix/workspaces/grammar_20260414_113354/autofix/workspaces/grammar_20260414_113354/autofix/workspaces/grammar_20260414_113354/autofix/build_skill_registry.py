from __future__ import annotations
import json
from pathlib import Path

BASE = Path("/home/sibilla-cumana/ralfloop_agent_scaffold")
TARGETS = BASE / "autofix" / "targets"
OUT = BASE / "openshell_backend" / "skills" / "registry.py"

targets = []
for p in sorted(TARGETS.glob("*.json")):
    obj = json.loads(p.read_text(encoding="utf-8"))
    ds = obj.get("direct_skill") or {}
    mod = str(ds.get("module") or "").strip()
    fn = str(ds.get("function") or "").strip()
    name = str(obj.get("name") or p.stem).strip()
    if mod and fn and name:
        targets.append((name, mod, fn))

imports = []
entries = []
for name, mod, fn in targets:
    alias = f"{name}__fn".replace("-", "_")
    imports.append(f"from {mod} import {fn} as {alias}")
    entries.append(f'    ("{name}", {alias}),')

content = """from __future__ import annotations

from typing import Callable

""" + "\n".join(imports) + """

SkillFn = Callable[[str], str | None]

DIRECT_SKILLS: list[tuple[str, SkillFn]] = [
""" + "\n".join(entries) + """
]

def try_direct_skill(user_goal: str) -> tuple[str, str] | None:
    text = (user_goal or "").strip()
    if not text:
        return None

    for name, fn in DIRECT_SKILLS:
        try:
            out = fn(text)
        except Exception:
            out = None
        if out:
            return name, out

    return None
"""
OUT.write_text(content, encoding="utf-8")
print(f"REGISTRY_BUILT {OUT}")
