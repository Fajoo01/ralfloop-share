from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class Diagnosis:
    kind: str
    target_file: str
    target_symbol: str
    why: str
    confidence: float



def _diagnose_grammar_upstream(validation: dict[str, Any] | None):
    if not isinstance(validation, dict):
        return None

    upstream = validation.get("upstream_diagnostic")
    raw_issues = []
    if isinstance(upstream, dict) and upstream.get("kind") == "grammar_analysis_suspicions":
        raw_issues = upstream.get("raw_issues") or []
    elif isinstance(validation.get("raw_issues"), list) and validation.get("raw_issues"):
        raw_issues = validation.get("raw_issues") or []
    else:
        return None

    first = raw_issues[0] if raw_issues else {}
    issue_kind = first.get("kind") if isinstance(first, dict) else None

    target_file = "openshell_backend/skill_grammar_rag.py"
    target_symbol = "_simple_local_grammar_fallback"
    why = "grammar raw issues indicate tokenizer/fallback defect"

    if issue_kind == "suspicious_apostrophe_token":
        why = "apostrophe token classified as nome_comune before repair; patch tokenizer/fallback before classification"

    return {
        "kind": "grammar_upstream_issue",
        "confidence": 0.9,
        "target_file": target_file,
        "target_symbol": target_symbol,
        "why": why,
    }


def diagnose_skill_failure(
    *,
    skill_name: str,
    final_answer: str | None,
    validation: dict[str, Any] | None,
    audit_summary: list[str] | None,
) -> Diagnosis:
    skill = str(skill_name or "").strip().lower()
    validation = validation or {}

    grammar_diag = _diagnose_grammar_upstream(validation)
    if grammar_diag is not None:
        return Diagnosis(**grammar_diag)
    audit_summary = audit_summary or []

    reason = str(validation.get("reason") or "").strip().lower()
    details = validation.get("details") or []

    if skill == "grammar":
        if reason in {"invalid_json", "empty_or_non_list", "missing_tokens", "no_valid_grammar_entries"}:
            return Diagnosis(
                kind="skill_bug",
                target_file="openshell_backend/skill_grammar_rag.py",
                target_symbol="run_grammar_skill",
                why=f"grammar output invalid or insufficient: {reason}",
                confidence=0.87,
            )

        if reason in {"no_relevant_tokens_found", "validator_could_not_apply"}:
            return Diagnosis(
                kind="validator_bug",
                target_file="openshell_backend/skills/validators.py",
                target_symbol="validate_grammar_output",
                why=f"validator appears too specific for observed grammar output: {reason}",
                confidence=0.92,
            )

    if any("skill_output_insufficient" in str(x) for x in audit_summary):
        return Diagnosis(
            kind="contract_mismatch",
            target_file="openshell_backend/skills/validators.py",
            target_symbol="validate_skill_output",
            why="skill output marked insufficient but no specific diagnosis matched",
            confidence=0.65,
        )

    return Diagnosis(
        kind="runtime_bug",
        target_file="ralfloop_agent/core/loop.py",
        target_symbol="RalfloopAgent.run",
        why="fallback diagnosis",
        confidence=0.35,
    )

def diagnosis_to_dict(d: Diagnosis) -> dict[str, Any]:
    return {
        "kind": d.kind,
        "target_file": d.target_file,
        "target_symbol": d.target_symbol,
        "why": d.why,
        "confidence": d.confidence,
    }

