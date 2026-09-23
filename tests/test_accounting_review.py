from decimal import Decimal

from ralfloop_agent.unified_assistant.accounting_review import (
    build_human_review_proposal,
    build_missing_document_review_queue,
    reconstructed_total,
    review_missing_document_case,
)
from ralfloop_agent.unified_assistant.accounting import accounting_read_adapter
from ralfloop_agent.unified_assistant.contracts import PlanAssignment, PolicyClass


def case(**updates):
    row = {
        "movement_id": "mov-1",
        "amount": "42,50",
        "counterparty": "Cartoleria fixture",
        "proposed_category": "materiale didattico",
        "original_document_refs": [],
        "payment_refs": ["bank:tx-1"],
        "context_refs": ["email:order-1"],
    }
    row.update(updates)
    return row


def test_original_receipt_is_documented_without_human_reconstruction():
    row = review_missing_document_case(case(original_document_refs=["doc:receipt-1"]))
    assert row["original_document_status"] == "PRESENT"
    assert row["accounting_status"] == "DOCUMENTED"
    assert row["bookkeeping_postable"] is True
    assert row["human_review_required"] is False


def test_bank_plus_context_can_be_proposed_but_never_becomes_a_receipt():
    row = review_missing_document_case(case())
    assert row["evidence_status"] == "CORROBORATED_MISSING_ORIGINAL"
    assert row["original_document_status"] == "MISSING"
    assert row["accounting_status"] == "REVIEW_REQUIRED"
    assert row["bookkeeping_postable"] is False
    assert row["human_review_required"] is True
    assert row["external_eligibility"] == "REQUIRES_SEPARATE_RULE_CHECK"


def test_human_can_approve_reconstruction_without_fabricating_document():
    row = review_missing_document_case(case(human_decision="approve_reconstruction"))
    assert row["accounting_status"] == "HUMAN_APPROVED_RECONSTRUCTION"
    assert row["bookkeeping_postable"] is True
    assert row["human_review_required"] is False
    assert row["original_document_status"] == "MISSING"
    assert row["original_document_refs"] == []
    assert row["external_eligibility"] == "REQUIRES_SEPARATE_RULE_CHECK"


def test_human_can_keep_movement_but_mark_it_nonreportable():
    row = review_missing_document_case(case(human_decision="approve_nonreportable"))
    assert row["accounting_status"] == "HUMAN_APPROVED_NONREPORTABLE"
    assert row["bookkeeping_postable"] is True
    assert row["external_eligibility"] == "EXCLUDED_BY_HUMAN_DECISION"
    assert row["original_document_status"] == "MISSING"


def test_no_primary_payment_evidence_stays_blocked_even_if_human_tries_to_approve():
    row = review_missing_document_case(case(payment_refs=[], human_decision="approve_reconstruction"))
    assert row["accounting_status"] == "BLOCKED_NO_PRIMARY_EVIDENCE"
    assert row["bookkeeping_postable"] is False
    assert row["original_document_status"] == "MISSING"


def test_review_queue_counts_human_intervention_and_never_invents_documents():
    queue = build_missing_document_review_queue([
        case(movement_id="a"),
        case(movement_id="b", human_decision="approve_reconstruction"),
        case(movement_id="c", original_document_refs=["doc:c"]),
        case(movement_id="d", payment_refs=[]),
    ])
    assert queue["case_count"] == 4
    assert queue["review_required_count"] == 2
    assert queue["human_approved_reconstruction_count"] == 1
    assert queue["missing_original_count"] == 3
    assert queue["invariants"]["invented_documents"] == 0
    assert reconstructed_total(queue) == Decimal("85.00")


def test_commercialista_adapter_exposes_human_review_queue():
    assignment = PlanAssignment(
        task_id="task.accounting-review-fixture",
        domain="accounting",
        skill="accounting.read",
        objective="Controlla le ricevute perse e fammi quadrare i giustificativi",
        input_refs=("user.goal",),
        output_ref="artifact.accounting",
        policy=PolicyClass.READ,
    )
    artifact = accounting_read_adapter(
        assignment,
        {"accounting.document_cases": [case()]},
    )
    assert artifact.status == "clarification_required"
    assert artifact.payload["human_review_required"] is True
    assert artifact.payload["document_review_queue"]["review_required_count"] == 1
    assert artifact.payload["writes"] == 0
    assert artifact.payload["payments"] == 0
    assert "nessuna ricevuta viene inventata" in artifact.payload["message"]


def test_commercialista_adapter_completes_after_explicit_human_approval():
    assignment = PlanAssignment(
        task_id="task.accounting-review-approved",
        domain="accounting",
        skill="accounting.read",
        objective="Rivedi la ricevuta persa",
        input_refs=("user.goal",),
        output_ref="artifact.accounting",
        policy=PolicyClass.READ,
    )
    artifact = accounting_read_adapter(
        assignment,
        {"accounting.document_cases": [case(human_decision="approve_reconstruction")]},
    )
    assert artifact.status == "completed"
    row = artifact.payload["document_review_queue"]["rows"][0]
    assert row["accounting_status"] == "HUMAN_APPROVED_RECONSTRUCTION"
    assert row["original_document_status"] == "MISSING"


def test_pending_case_becomes_tiremm_admin_approval_proposal():
    row = review_missing_document_case(case())
    proposal = build_human_review_proposal(row, evidence_ids=("src.1234567890abcdef12345678",))
    assert proposal.action == "propose_update"
    assert proposal.requires_approval is True
    assert proposal.target == "accounting:mov-1"
    assert proposal.payload["original_document_status"] == "MISSING"
    assert proposal.payload["invented_documents"] == 0
    assert "approve_reconstruction" in proposal.payload["allowed_decisions"]
