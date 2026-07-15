from __future__ import annotations

from typing import Any

JURY_REASON_CODES = {
    "qualitative_judgment",
    "strategic_assessment",
    "recommendation_required",
    "conflicting_sources",
    "incomplete_rules",
    "evidence_synthesis",
    "domain_validation",
    "domain_creation_review",
    "unresolved_exception",
    "mixed_request_unresolved",
}
RECURSIVE_REASON_CODES = {
    "qualitative_judgment",
    "strategic_assessment",
    "recommendation_required",
    "conflicting_sources",
    "incomplete_rules",
    "evidence_synthesis",
    "domain_validation",
    "domain_creation_review",
}
NO_JURY = {
    "deterministic_complete",
    "lookup_complete",
    "exact_lookup",
    "formula_complete",
    "arithmetic_basic",
    "strict_transformation",
    "code_fix_deterministic",
    "policy_denied",
    "out_of_scope",
    "human_confirmation_pending",
    "domain_creation_required",
}


class DomainJuryRouter:
    def should_use_jury(
        self,
        *,
        domain_resolution: dict[str, Any],
        classification: str,
        deterministic_result: dict[str, Any] | None = None,
        human_confirmation_pending: bool = False,
        requested_reason_codes: list[str] | None = None,
        domain_creation_review: bool = False,
    ) -> dict[str, Any]:
        deterministic_result = deterministic_result or {}
        classification = str(classification or "")
        if human_confirmation_pending or classification == "external_action":
            return _off("human_confirmation_pending")
        if classification in {"out_of_scope", "policy_denied"}:
            return _off(classification)
        if deterministic_result.get("complete"):
            return _off("deterministic_complete")
        if classification in {"deterministic", "lookup_complete", "exact_lookup", "arithmetic_basic", "strict_transformation", "code_fix_deterministic"}:
            return _off("lookup_complete" if "lookup" in classification else classification)

        resolution_status = str(domain_resolution.get("status") or "missing")
        if resolution_status != "resolved" and not domain_creation_review:
            return _off("domain_creation_required")

        reasons = [str(reason) for reason in requested_reason_codes or []]
        if classification in RECURSIVE_REASON_CODES:
            reasons.append(classification)
        if deterministic_result.get("conflicts"):
            reasons.append("conflicting_sources")
        unresolved = [str(item) for item in deterministic_result.get("unresolved_questions") or []]
        reasons.extend(reason for reason in unresolved if reason in RECURSIVE_REASON_CODES)
        if "recommendation_required" in unresolved or classification == "non_deterministic":
            reasons.extend(("recommendation_required", "qualitative_judgment"))
        if classification == "mixed" and unresolved:
            reasons.append("mixed_request_unresolved")
        if not deterministic_result.get("matched_rules") and classification in {"mixed", "insufficient_evidence"}:
            reasons.append("incomplete_rules")
        if domain_creation_review:
            reasons.append("domain_creation_review")
        reasons = [reason for reason in dict.fromkeys(reasons) if reason in JURY_REASON_CODES]
        if not reasons:
            return _off("incomplete_rules")
        return {
            "use_jury": True,
            "reason_codes": reasons,
            "jury_mode": "required",
            "rounds": 2,
            "roles": ["planner", "critic", "solver"],
            "domain_context_required": not domain_creation_review,
        }


def _off(reason: str) -> dict[str, Any]:
    return {
        "use_jury": False,
        "reason_codes": [reason],
        "jury_mode": "off",
        "rounds": 0,
        "roles": [],
        "domain_context_required": False,
    }
