from __future__ import annotations

import json
import os
from pathlib import Path
import sys

SOURCE_ROOT = Path(__file__).resolve().parents[1]
LIVE_ROOT = Path("/home/sibilla-cumana/ralfloop-production/current")
ROOT = Path(
    os.getenv("RALFLOOP_RELEASE_ROOT")
    or (SOURCE_ROOT if (SOURCE_ROOT / "src").is_dir() else LIVE_ROOT)
).resolve()
CONFIG = ROOT / "config" / "capability_routing.json"
QUERIES = (
    "analizza la strategia relazionale",
    "come è messa la curva relazionale?",
    "mostrami gli ultimi eventi",
    "analizza la situazione usando anche dialogo strategico e manuali di psicologia",
)


def validate() -> None:
    payload = json.loads(CONFIG.read_text(encoding="utf-8"))
    names = {str(item.get("name")) for item in payload.get("read_mcp_keywords", [])}
    if "abc_relation" not in names:
        raise RuntimeError("abc_relation missing from read_mcp_keywords")
    root = str(ROOT)
    if root not in sys.path:
        sys.path.insert(0, root)
    from src.router import route_task

    for query in QUERIES:
        route = route_task(query)
        if "abc_relation" not in route.mcp_used or route.mode == "external_action":
            raise RuntimeError(f"abc_relation routing canary failed: {query!r}")


if __name__ == "__main__":
    validate()
    print("ABC_ROUTING_CANARY_OK")