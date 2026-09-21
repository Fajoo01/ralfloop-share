#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json

from ralfloop_agent.abc_relation.intake import build_proposal, classify_text, propose_event
from ralfloop_agent.abc_relation.models import SourceKind


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Convert one natural-language ABC update into a guarded event proposal."
    )
    parser.add_argument("text", help="Natural update, e.g. 'oggi abbiamo cenato insieme'")
    parser.add_argument("--occurred-at", help="Timezone-aware ISO timestamp; defaults to local current time")
    parser.add_argument("--actor")
    parser.add_argument(
        "--source-kind",
        choices=[item.value for item in SourceKind],
        default=SourceKind.MANUAL.value,
    )
    parser.add_argument("--source-ref", default="manual:natural_intake")
    args = parser.parse_args()

    output = build_proposal(
        args.text,
        occurred_at=args.occurred_at,
        actor=args.actor,        source_kind=args.source_kind,
        source_ref=args.source_ref,
    )
    print(json.dumps(output, ensure_ascii=False, indent=2))
    return 0


__all__ = ["main", "classify_text", "propose_event"]


if __name__ == "__main__":
    raise SystemExit(main())