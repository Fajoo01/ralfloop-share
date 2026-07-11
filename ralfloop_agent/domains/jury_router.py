from __future__ import annotations

from typing import Any

JURY_REASON_CODES = {
    "domain_ambiguous", "qualitative_judgment", "conflicting_sources", "unresolved_exception",
    "incomplete_rules", "mixed_request_unresolved", "recommendation_required", "strategic_assessment",
    "evidence_synthesis", "domain_validation", "domain_creation_review",
}
NO_JURY = {"deterministic_complete", "exact_lookup", "formula_complete", "policy_denied", "out_of_scope", "human_confirmation_pending"}


class DomainJuryRouter:
    def should_use_jury(self, *, domain_resolution: dict[str, Any], classification: str, deterministic_result: dict[str, Any] | None = None, human_confirmation_pending: bool = False) -> dict[str, Any]:
        deterministic_result = deterministic_result or {}
        if human_confirmation_pending:
            return _off("human_confirmation_pending")
        if classification == "out_of_scope":
            return _off("out_of_scope")
        if deterministic_result.get("complete"):
            return _off("deterministic_complete")
        reasons = []
        if domain_resolution.get("status") == "ambiguous":
            reasons.append("domain_ambiguous")
        if deterministic_result.get("conflicts"):
            reasons.append("conflicting_sources")
        unresolved = deterministic_result.get("unresolved_questions") or []
        if "recommendation_required" in unresolved or classification == "non_deterministic":
            reasons.append("recommendation_required")
            reasons.append("qualitative_judgment")
        if classification == "mixed" and unresolved:
            reasons.append("mixed_request_unresolved")
        if not deterministic_result.get("matched_rules") and classification in {"mixed", "insufficient_evidence"}:
            reasons.append("incomplete_rules")
        reasons = [r for r in dict.fromkeys(reasons) if r in JURY_REASON_CODES]
        if not reasons:
            return _off("deterministic_complete" if deterministic_result.get("complete") else "incomplete_rules")
        return {"use_jury": True, "reason_codes": reasons, "jury_mode": "required", "rounds": 1, "roles": ["domain_expert", "evidence_analyst", "rule_auditor", "adversarial_reviewer", "final_synthesizer"], "domain_context_required": True}


def _off(reason: str) -> dict[str, Any]:
    return {"use_jury": False, "reason_codes": [reason], "jury_mode": "off", "rounds": 0, "roles": [], "domain_context_required": False}
