#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from ralfloop_agent.abc_relation.importer import import_legacy_bundle
from ralfloop_agent.abc_relation.service import RelationService
from ralfloop_agent.abc_relation.store import RelationStore


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Import legacy ABC JSON snapshots into the structured relation store.")
    parser.add_argument("paths", nargs="+", help="Legacy JSON files. Raw WhatsApp exports are intentionally not accepted here.")
    parser.add_argument(
        "--db",
        default=str(Path.home() / ".local" / "share" / "ralfloop" / "abc_relation.sqlite3"),
        help="SQLite destination path.",
    )
    args = parser.parse_args(argv)
    service = RelationService(RelationStore(Path(args.db).expanduser()))
    result = import_legacy_bundle(service, args.paths)
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
