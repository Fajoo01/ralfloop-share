from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from typing import Any, Iterable, Mapping

from .models import EvidenceKind, SourceKind

ALLOWED_FIELDS = {
    "occurred_at", "kind", "summary", "source_kind", "source_ref", "actor",
    "confidence", "weight", "tags", "raw_excerpt", "source_hash",
    "supersedes_event_id",
}
REQUIRED_FIELDS = {"occurred_at", "kind", "summary", "source_kind", "source_ref"}


def _aware_datetime(raw: str) -> str:
    value = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("occurred_at must include timezone")
    return value.isoformat()


def normalize_event(row: Mapping[str, Any]) -> dict[str, Any]:
    unknown = set(row) - ALLOWED_FIELDS
    missing = REQUIRED_FIELDS - set(row)
    if unknown:
        raise ValueError(f"unknown fields: {sorted(unknown)}")
    if missing:
        raise ValueError(f"missing fields: {sorted(missing)}")

    normalized = dict(row)
    normalized["occurred_at"] = _aware_datetime(str(row["occurred_at"]))
    normalized["kind"] = EvidenceKind(str(row["kind"])).value
    normalized["source_kind"] = SourceKind(str(row["source_kind"])).value
    normalized["summary"] = str(row["summary"]).strip()
    normalized["source_ref"] = str(row["source_ref"]).strip()
    if not normalized["summary"] or not normalized["source_ref"]:
        raise ValueError("summary and source_ref must be non-empty")

    confidence = float(row.get("confidence", 1.0))
    weight = float(row.get("weight", 0.0))
    if not 0.0 <= confidence <= 1.0:
        raise ValueError("confidence out of range")
    if not -100.0 <= weight <= 100.0:
        raise ValueError("weight out of range")
    normalized["confidence"] = confidence
    normalized["weight"] = weight
    tags = row.get("tags", [])
    if not isinstance(tags, list):
        raise ValueError("tags must be a list")
    normalized["tags"] = [str(tag).strip() for tag in tags if str(tag).strip()]

    raw_excerpt = row.get("raw_excerpt")
    if raw_excerpt is not None and len(str(raw_excerpt)) > 600:
        raise ValueError("raw_excerpt exceeds 600 characters")
    if raw_excerpt is not None:
        normalized["raw_excerpt"] = str(raw_excerpt)
    actor = row.get("actor")
    if actor is not None:
        normalized["actor"] = str(actor)
    return normalized


def proposal_digest(events: list[dict[str, Any]]) -> str:
    canonical = json.dumps(
        events,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def require_confirmed_digest(events: list[dict[str, Any]], confirmed: str | None) -> str:
    digest = proposal_digest(events)
    if not confirmed:
        raise ValueError("--commit requires --confirm-digest from the matching dry-run")
    if confirmed != digest:
        raise ValueError("confirmed digest does not match current normalized proposal")
    return digest


INFERENCE_PATTERNS = (
    r"\bmi sembra\b", r"\bsembra che\b", r"\bsembrava\b", r"\bsecondo me\b",
    r"\bcredo che\b", r"\bpenso che\b", r"\bforse\b", r"\bprobabilmente\b",
    r"\bmi pare\b", r"\bho l['’]impressione\b",
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
        r"^(?:(?:abc\s*(?:relazione)?|aggiorna\s+abc|proponi\s+evento\s+abc|registra\s+evento\s+relazionale)\s*[:,-]?\s*)",
        "",
        value,
        flags=re.IGNORECASE,
    ).strip()
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
        return (
            "inference", 0.45,
            ["natural_intake", "auto_classified", "needs_review"],
            "interpretive_marker",
        )
    if _matches(BOUNDARY_PATTERNS, summary):
        return (
            "boundary", 0.9,
            ["natural_intake", "auto_classified", "explicit_boundary"],
            "explicit_boundary_marker",
        )
    return (
        "observed_fact", 0.9,
        ["natural_intake", "auto_classified"],
        "default_observable",
    )


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
    review = {
        "classification_reason": reason,
        "auto_classified": True,
        "requires_human_review": kind == "inference",
    }
    return event, review


def build_proposal(text: str, **kwargs: Any) -> dict[str, Any]:
    event, review = propose_event(text, **kwargs)
    events = [event]
    return {
        "events": events,
        "proposal_digest": proposal_digest(events),
        "review": review,
        "side_effects": "none",
    }