from __future__ import annotations

from pathlib import Path
from typing import Any

from .storage import sha256_file

SOURCE_TYPES = {
    "official_manual", "law_or_regulation", "internal_policy", "technical_standard",
    "official_documentation", "validated_dataset", "approved_local_policy",
    "expert-authored_document", "ralfloop_local_policy",
}
INSUFFICIENT_ALONE = {"LLM_generated_text", "unsourced_notes", "anonymous_post", "search_snippet", "unverified_summary"}


def source_record(source_id: str, path: Path, source_type: str = "expert-authored_document") -> dict[str, Any]:
    return {
        "source_id": source_id,
        "title": path.name,
        "source_type": source_type,
        "uri_or_path": str(path),
        "publisher": "local",
        "version": "",
        "published_at": None,
        "retrieved_at": None,
        "checksum": sha256_file(path) if path.exists() else "",
        "reliability": 0.8 if source_type in SOURCE_TYPES else 0.2,
        "sections_used": [],
        "notes": "",
    }


def validate_rule_provenance(rule: dict[str, Any], source_ids: set[str], sources_by_id: dict[str, dict[str, Any]] | None = None) -> list[str]:
    sources_by_id = sources_by_id or {}
    rule_type = rule.get("rule_type", "source_derived")
    refs = rule.get("source_refs") or []
    if rule_type == "local_operational_policy":
        if not refs:
            return [f"rule_without_local_policy_source:{rule.get('rule_id')}"]
        wrong = [
            ref for ref in refs
            if sources_by_id.get(ref) and sources_by_id[ref].get("source_type") != "ralfloop_local_policy"
        ]
        return [f"local_policy_source_type_invalid:{rule.get('rule_id')}:{ref}" for ref in wrong]
    if not refs:
        return [f"rule_without_provenance:{rule.get('rule_id')}"]
    missing = [ref for ref in refs if ref not in source_ids]
    issues = [f"rule_source_missing:{rule.get('rule_id')}:{ref}" for ref in missing]
    for ref in refs:
        source_type = sources_by_id.get(ref, {}).get("source_type")
        if source_type in INSUFFICIENT_ALONE:
            issues.append(f"rule_source_insufficient:{rule.get('rule_id')}:{ref}:{source_type}")
    return issues
