from __future__ import annotations

from dataclasses import asdict, dataclass, field

from .source_authority import SourceAuthorityAssessment, SourceCandidate


@dataclass
class SourceJuryAssessment:
    candidate_id: str
    authority_level: str
    authority_score: float
    official: bool
    binding_eligible: bool
    issuer_match: bool
    version_match: bool
    document_type: str
    reasons: list[str] = field(default_factory=list)
    concerns: list[str] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)
    recommended_use: str = "reject"
    jury_consensus: float = 0.0
    human_review_required: bool = False
    roles: list[str] = field(default_factory=lambda: [
        "issuer_identity_reviewer",
        "official_source_verifier",
        "document_version_reviewer",
        "rule_provenance_reviewer",
        "conflict_reviewer",
        "adversarial_source_reviewer",
        "final_source_synthesizer",
    ])

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class SourceConflictAssessment:
    status: str
    local_checksum: str = ""
    web_checksum: str = ""
    local_version: str | None = None
    web_version: str | None = None
    reasons: list[str] = field(default_factory=list)
    human_review_required: bool = False

    def to_dict(self) -> dict:
        return asdict(self)


class SourceJury:
    """Deterministic source jury shim. It cannot override authority hard gates."""

    def assess(self, candidate: SourceCandidate, authority: SourceAuthorityAssessment) -> SourceJuryAssessment:
        reasons = list(authority.reasons)
        concerns = list(authority.concerns)
        if "hard_gate_failed" in concerns:
            recommended = "reject" if authority.authority_level == "D" else "discovery_only"
            score = 0.0 if recommended == "reject" else 0.35
            concerns.append("jury_cannot_override_hard_gate")
            return SourceJuryAssessment(
                candidate.candidate_id,
                authority.authority_level,
                score,
                False,
                False,
                authority.issuer_match,
                bool(candidate.version or candidate.publication_date),
                authority.document_type,
                reasons,
                concerns,
                [],
                recommended,
                0.75,
                True,
            )
        if authority.authority_level in {"A", "B"} and authority.binding_eligible:
            recommended = "binding"
            score = 0.95 if authority.authority_level == "A" else 0.82
            official = True
            review = not authority.issuer_match or not bool(candidate.version or candidate.publication_date)
        elif authority.authority_level == "C":
            recommended = "supporting"
            score = 0.55
            official = False
            review = True
            concerns.append("supporting_source_not_binding")
        else:
            recommended = "reject"
            score = 0.1
            official = False
            review = True
            concerns.append("unverified_source")
        if not authority.issuer_match:
            concerns.append("issuer_mismatch")
        if not (candidate.version or candidate.publication_date):
            concerns.append("version_or_date_missing")
        return SourceJuryAssessment(
            candidate.candidate_id,
            authority.authority_level,
            score,
            official,
            authority.binding_eligible and recommended == "binding",
            authority.issuer_match,
            bool(candidate.version or candidate.publication_date),
            authority.document_type,
            reasons,
            concerns,
            [],
            recommended,
            0.8,
            review,
        )

    def assess_conflict(self, local: dict, web: dict) -> SourceConflictAssessment:
        local_checksum = str(local.get("checksum") or "")
        web_checksum = str(web.get("checksum") or "")
        local_version = local.get("version")
        web_version = web.get("version")
        if local_checksum and web_checksum and local_checksum == web_checksum:
            return SourceConflictAssessment("same_document", local_checksum, web_checksum, local_version, web_version)
        if not web_checksum:
            return SourceConflictAssessment("unverifiable_copy", local_checksum, web_checksum, local_version, web_version, ["web_checksum_missing"], True)
        if local_version and web_version and local_version != web_version:
            return SourceConflictAssessment("newer_official_version", local_checksum, web_checksum, local_version, web_version, ["version_changed"], True)
        if local_checksum and web_checksum and local_checksum != web_checksum:
            return SourceConflictAssessment("different_document", local_checksum, web_checksum, local_version, web_version, ["checksum_changed"], True)
        return SourceConflictAssessment("unverifiable_copy", local_checksum, web_checksum, local_version, web_version, ["insufficient_metadata"], True)
