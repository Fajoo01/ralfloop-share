from __future__ import annotations

from decimal import Decimal
from typing import Any, Mapping

from .accounting import parse_eur


_ALLOWED_HUMAN_DECISIONS = {
    "approve_reconstruction",
    "approve_nonreportable",
    "reject",
}


def _refs(case: Mapping[str, Any], key: str) -> tuple[str, ...]:
    raw = case.get(key) or ()
    if isinstance(raw, str):
        raw = (raw,)
    return tuple(dict.fromkeys(str(item).strip() for item in raw if str(item).strip()))


def review_missing_document_case(case: Mapping[str, Any]) -> dict[str, Any]:
    """Assess a bookkeeping movement whose original supporting document may be missing.

    The function may reconstruct bookkeeping context from source-backed evidence, but it
    never upgrades indirect evidence into an original receipt/invoice. Human approval is
    represented separately from documentary status and never implies tax/grant eligibility.
    """
    movement_id = str(case.get("movement_id") or "").strip()
    if not movement_id:
        raise ValueError("movement_id_required")
    amount = parse_eur(case.get("amount"))
    if not amount.is_finite() or amount < 0:
        raise ValueError("amount_invalid")

    original_refs = _refs(case, "original_document_refs")
    payment_refs = _refs(case, "payment_refs")
    context_refs = _refs(case, "context_refs")
    evidence_refs = tuple(dict.fromkeys((*original_refs, *payment_refs, *context_refs)))
    human_decision = str(case.get("human_decision") or "").strip() or None
    if human_decision is not None and human_decision not in _ALLOWED_HUMAN_DECISIONS:
        raise ValueError("human_decision_invalid")

    if original_refs:
        evidence_status = "DOCUMENTED"
        original_document_status = "PRESENT"
        human_review_required = False
        accounting_status = "DOCUMENTED"
        bookkeeping_postable = True
    elif payment_refs and context_refs:
        evidence_status = "CORROBORATED_MISSING_ORIGINAL"
        original_document_status = "MISSING"
        human_review_required = True
        accounting_status = "REVIEW_REQUIRED"
        bookkeeping_postable = False
    elif payment_refs:
        evidence_status = "PAYMENT_ONLY_MISSING_ORIGINAL"
        original_document_status = "MISSING"
        human_review_required = True
        accounting_status = "REVIEW_REQUIRED"
        bookkeeping_postable = False
    else:
        evidence_status = "UNSUPPORTED_MISSING_ORIGINAL"
        original_document_status = "MISSING"
        human_review_required = True
        accounting_status = "BLOCKED_NO_PRIMARY_EVIDENCE"
        bookkeeping_postable = False

    if original_document_status == "MISSING" and human_decision == "approve_reconstruction":
        if not payment_refs:
            accounting_status = "BLOCKED_NO_PRIMARY_EVIDENCE"
        else:
            accounting_status = "HUMAN_APPROVED_RECONSTRUCTION"
            bookkeeping_postable = True
            human_review_required = False
    elif original_document_status == "MISSING" and human_decision == "approve_nonreportable":
        if not payment_refs:
            accounting_status = "BLOCKED_NO_PRIMARY_EVIDENCE"
        else:
            accounting_status = "HUMAN_APPROVED_NONREPORTABLE"
            bookkeeping_postable = True
            human_review_required = False
    elif human_decision == "reject":
        accounting_status = "REJECTED_BY_HUMAN"
        bookkeeping_postable = False
        human_review_required = False

    if original_document_status == "PRESENT":
        external_eligibility = "REQUIRES_RULE_CHECK"
    elif accounting_status == "HUMAN_APPROVED_NONREPORTABLE":
        external_eligibility = "EXCLUDED_BY_HUMAN_DECISION"
    else:
        external_eligibility = "REQUIRES_SEPARATE_RULE_CHECK"

    return {
        "movement_id": movement_id,
        "amount_eur": str(amount),
        "proposed_category": str(case.get("proposed_category") or "").strip() or None,
        "counterparty": str(case.get("counterparty") or "").strip() or None,
        "original_document_status": original_document_status,
        "evidence_status": evidence_status,
        "accounting_status": accounting_status,
        "bookkeeping_postable": bookkeeping_postable,
        "human_review_required": human_review_required,
        "human_decision": human_decision,
        "external_eligibility": external_eligibility,
        "evidence_refs": list(evidence_refs),
        "original_document_refs": list(original_refs),
        "payment_refs": list(payment_refs),
        "context_refs": list(context_refs),
        "audit_note": (
            "Original document present. Classification/eligibility still requires the applicable rule set."
            if original_document_status == "PRESENT"
            else "Original document missing. Indirect evidence and human approval never become an invented receipt/invoice."
        ),
    }


def build_missing_document_review_queue(cases: list[Mapping[str, Any]]) -> dict[str, Any]:
    rows = [review_missing_document_case(case) for case in cases]
    review_required = [row for row in rows if row["human_review_required"]]
    postable = [row for row in rows if row["bookkeeping_postable"]]
    blocked = [row for row in rows if row["accounting_status"] == "BLOCKED_NO_PRIMARY_EVIDENCE"]
    reconstructed = [row for row in rows if row["accounting_status"] == "HUMAN_APPROVED_RECONSTRUCTION"]
    missing_original = [row for row in rows if row["original_document_status"] == "MISSING"]
    return {
        "case_count": len(rows),
        "review_required_count": len(review_required),
        "postable_count": len(postable),
        "blocked_count": len(blocked),
        "human_approved_reconstruction_count": len(reconstructed),
        "missing_original_count": len(missing_original),
        "rows": rows,
        "invariants": {
            "invented_documents": 0,
            "human_approval_required_for_missing_original": True,
            "bookkeeping_reconstruction_does_not_imply_tax_or_grant_eligibility": True,
        },
    }


def build_human_review_proposal(review: Mapping[str, Any], *, evidence_ids: tuple[str, ...]):
    """Create a Tiremm Admin approval-bound proposal for a pending accounting case."""
    if not review.get("human_review_required"):
        raise ValueError("human_review_not_required")
    if not evidence_ids:
        raise ValueError("human_review_evidence_required")
    from .tiremm_admin import ActionProposal

    movement_id = str(review.get("movement_id") or "").strip()
    if not movement_id:
        raise ValueError("movement_id_required")
    return ActionProposal(
        action="propose_update",
        target=f"accounting:{movement_id}",
        payload={
            "movement_id": movement_id,
            "amount_eur": str(review.get("amount_eur") or ""),
            "proposed_category": review.get("proposed_category"),
            "original_document_status": review.get("original_document_status"),
            "evidence_status": review.get("evidence_status"),
            "allowed_decisions": [
                "approve_reconstruction",
                "approve_nonreportable",
                "reject",
            ],
            "tax_or_grant_eligibility_not_implied": True,
            "invented_documents": 0,
        },
        evidence_ids=evidence_ids,
        reason="Original supporting document missing: human accounting decision required.",
    )


def reconstructed_total(queue: Mapping[str, Any]) -> Decimal:
    return sum(
        (
            parse_eur(row["amount_eur"])
            for row in queue.get("rows") or ()
            if row.get("bookkeeping_postable")
        ),
        Decimal("0"),
    )


__all__ = [
    "build_human_review_proposal",
    "build_missing_document_review_queue",
    "reconstructed_total",
    "review_missing_document_case",
]
