"""Source-backed document review proposals. No submission executor."""
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
from typing import Literal

from pydantic import Field

from .contracts import StrictModel
from .memory_service import MemoryEntity, MemoryService
from .platform import SourceRef


class DocumentReviewProposal(StrictModel):
    proposal_id: str
    practice_id: str = Field(min_length=1)
    authoritative_message_id: str = Field(min_length=1)
    document_id: str
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    action_required: str
    proposed_reply: str
    attachments: tuple[SourceRef, ...]
    provenance: tuple[SourceRef, ...] = Field(min_length=1)
    preconditions: tuple[str, ...]
    postconditions: tuple[str, ...]
    blockers: tuple[str, ...]
    status: Literal["BLOCKED_REVIEW"] = "BLOCKED_REVIEW"
    executable: Literal[False] = False


def cash_bridge(*, opening, management, excluded, unmapped, noncash_management, closing):
    values = [Decimal(str(x)) for x in (opening, management, excluded, unmapped, noncash_management, closing)]
    if not all(v.is_finite() for v in values):
        raise ValueError("reconciliation_amount_invalid")
    opening, management, excluded, unmapped, noncash_management, closing = values
    reconstructed = opening + management + excluded + unmapped - noncash_management
    return {"opening": str(opening), "management": str(management), "excluded": str(excluded),
            "unmapped": str(unmapped), "noncash_management": str(noncash_management),
            "closing": str(closing), "reconstructed": str(reconstructed),
            "residual": str(reconstructed - closing),
            "arithmetic_reconciled": reconstructed == closing,
            "classification_verified": False}


def prepare_document_review(memory: MemoryService, *, practice_id: str, message_id: str,
                            document: SourceRef, provenance: tuple[SourceRef, ...],
                            blockers: tuple[str, ...]) -> DocumentReviewProposal:
    if not document.content_hash or not any(s.system == "runts" and s.native_id == message_id for s in provenance):
        raise ValueError("authoritative_document_provenance_required")
    # This capability only prepares review drafts; it can never promote a filing.
    blockers = tuple(dict.fromkeys((*blockers, "FINAL_DOCUMENT_REVIEW_REQUIRED")))
    provenance = tuple(s for s in provenance if s.native_id != document.native_id) + (document,)
    digest = hashlib.sha256(f"{practice_id}|{message_id}|{document.content_hash}|{blockers}".encode()).hexdigest()
    proposal = DocumentReviewProposal(
        proposal_id="proposal.runts.document." + digest[:24], practice_id=practice_id,
        authoritative_message_id=message_id, document_id=document.native_id,
        sha256=document.content_hash, action_required="REVIEW_CORRECTED_BALANCE_BEFORE_MESSAGE_REPLY",
        proposed_reply="Bozza: in riscontro alla comunicazione dell'Ufficio, si propone la trasmissione del rendiconto per cassa rettificato tramite la messaggistica della pratica esistente. Non inviare finché le verifiche contabili e documentali non sono concluse.",
        attachments=(document,), provenance=provenance,
        preconditions=("authoritative_message_reread", "accounting_classifications_verified", "cash_bank_reconciled", "ministerial_structure_validated", "document_hash_unchanged", "official_runts_session_valid", "explicit_submission_authorization_required"),
        postconditions=("same_practice_message_reply_verified", "attachment_hash_verified", "no_new_deposit", "audit_and_memory_updated"),
        blockers=blockers,
    )
    memory.put_entity(MemoryEntity.build(entity_id=proposal.proposal_id, domain="runts",
        entity_type="RUNTS_DOCUMENT_PROPOSAL", status=proposal.status,
        updated_at=datetime.now(timezone.utc), data=proposal.model_dump(mode="json"), provenance=provenance))
    return proposal
