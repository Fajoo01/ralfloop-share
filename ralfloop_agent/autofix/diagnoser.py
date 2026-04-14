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


def diagnose_skill_failure(
    *,
    skill_name: str,
    final_answer: str | None,
    validation: dict[str, Any] | None,
    audit_summary: list[str] | None,
) -> Diagnosis:
    skill = str(skill_name or "").strip().lower()
    validation = validation or {}
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

