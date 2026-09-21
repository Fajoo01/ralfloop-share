#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
from datetime import datetime
from typing import Any, Iterable

from ralfloop_agent.abc_relation.models import SourceKind
from tools.abc_relation_record_events import normalize_event, proposal_digest

INFERENCE_PATTERNS = (
    r"\bmi sembra\b",
    r"\bsembra che\b",
    r"\bsembrava\b",
    r"\bsecondo me\b",
    r"\bcredo che\b",
    r"\bpenso che\b",
    r"\bforse\b",
    r"\bprobabilmente\b",
    r"\bmi pare\b",
    r"\bho l['’]impressione\b",
)

BOUNDARY_PATTERNS = (
    r"\bmi ha detto (?:di )?no\b",
    r"\bha rifiutato\b",
    r"\bmi ha chiesto di non\b",
    r"\bha detto che non vuole\b",
    r"\bnon vuole che\b",
    r"\bpreferisce non\b",
    r"\bnon se la sente\b",
    r"\bmi ha chiesto spazio\b",
    r"\bmi ha chiesto di lasciarl[ao] stare\b",
)


def _matches(patterns: Iterable[str], text: str) -> bool:
    return any(re.search(pattern, text, flags=re.IGNORECASE) for pattern in patterns)


def _clean_summary(text: str) -> str:
    value = " ".join(text.strip().split())
    value = re.sub(
        r"^(?:oggi\s*(?:è|e)\s*successo\s*(?:che)?\s*[:,-]?\s*|oggi\s*[:,-]\s*|oggi\s+)",
        "",
        value,
        flags=re.IGNORECASE,
    ).strip()
    if not value:
        raise ValueError("text must contain a concrete event or interpretation")
    return value


def classify_text(text: str) -> tuple[str, float, list[str], str]:
    summary = _clean_summary(text)
    if _matches(INFERENCE_PATTERNS, summary):
        return "inference", 0.45, ["natural_intake", "auto_classified", "needs_review"], "interpretive_marker"
    if _matches(BOUNDARY_PATTERNS, summary):
        return "boundary", 0.9, ["natural_intake", "auto_classified", "explicit_boundary"], "explicit_boundary_marker"
    return "observed_fact", 0.9, ["natural_intake", "auto_classified"], "default_observable"


def propose_event(
    text: str,
    *,
    occurred_at: str | None = None,
    actor: str | None = None,
    source_kind: str = SourceKind.MANUAL.value,
    source_ref: str = "manual:natural_intake",
) -> tuple[dict[str, Any], dict[str, Any]]:
    summary = _clean_summary(text)
    kind, confidence, tags, reason = classify_text(summary)
    row: dict[str, Any] = {
        "occurred_at": occurred_at or datetime.now().astimezone().isoformat(),
        "kind": kind,
        "summary": summary,
        "source_kind": source_kind,
        "source_ref": source_ref,
        "confidence": confidence,
        "weight": 0.0,
        "tags": tags,
        "raw_excerpt": text.strip()[:600],
    }
    if actor:
        row["actor"] = actor
    event = normalize_event(row)
    review = {"classification_reason": reason, "auto_classified": True, "requires_human_review": kind == "inference"}
    return event, review


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Convert one natural-language ABC update into a guarded event proposal."
    )
    parser.add_argument("text", help="Natural update, e.g. 'oggi abbiamo cenato insieme'")
    parser.add_argument("--occurred-at", help="Timezone-aware ISO timestamp; defaults to local current time")
    parser.add_argument("--actor")
    parser.add_argument("--source-kind", choices=[item.value for item in SourceKind], default=SourceKind.MANUAL.value)
    parser.add_argument("--source-ref", default="manual:natural_intake")
    args = parser.parse_args()

    event, review = propose_event(
        args.text,
        occurred_at=args.occurred_at,
        actor=args.actor,
        source_kind=args.source_kind,
        source_ref=args.source_ref,
    )
    events = [event]
    output = {
        "events": events,
        "proposal_digest": proposal_digest(events),
        "review": review,
    }
    print(json.dumps(output, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
