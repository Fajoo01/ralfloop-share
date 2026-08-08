from __future__ import annotations

from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

from .context import contains_secret
from .director import preflight_grant_review
from .models import ContextPacket, GlmReview, ValidationFinding, ValidationReport
from .normalizer import normalize_grant_review


def validate_review_artifact(
    packet: ContextPacket,
    review: GlmReview,
    source_payload: dict[str, Any],
    *,
    artifact_version: int = 1,
) -> ValidationReport:
    findings: list[ValidationFinding] = []
    checked = [
        "mandatory_requirements",
        "objectives_activities_kpi",
        "budget_totals",
        "dates_deadlines",
        "source_references",
        "missing_fields",
        "policy",
        "artifact_version",
        "secret_absence",
    ]
    if review.task_id != packet.task_id:
        findings.append(_error("task_id_mismatch", "Review task_id differs from the immutable packet."))
    if packet.task_type == "grant_review" and not packet.requirements:
        findings.append(_warning("requirements_missing", "No mandatory grant requirements were supplied."))
    draft_folded = packet.draft.casefold()
    uncovered = [item for item in packet.requirements if _keyword(item) not in draft_folded]
    if uncovered:
        findings.append(_warning("requirements_not_obviously_covered", f"Potentially uncovered requirements: {len(uncovered)}."))

    normalized_source = source_payload
    if packet.task_type == "grant_review":
        normalized_source, _ = normalize_grant_review(source_payload)
    proposal = normalized_source.get("proposal") if isinstance(normalized_source.get("proposal"), dict) else normalized_source
    proposal_fields_present: dict[str, bool] = {}
    if packet.task_type == "grant_review":
        for field in ("objectives", "activities", "kpis"):
            proposal_fields_present[field] = bool(proposal.get(field) or normalized_source.get(field)) or _semantic_field_present(field, packet)
            if not proposal_fields_present[field]:
                findings.append(_warning(f"{field}_missing", f"Grant proposal field '{field}' is missing."))
        _validate_budget(normalized_source.get("budget_summary") or packet.budget_summary, findings)
        _validate_dates(normalized_source, findings)

    source_ids = {item.source_id for item in packet.evidence}
    for issue in review.critical_issues:
        invented = sorted(set(issue.evidence_refs) - source_ids)
        if invented:
            findings.append(_error("invented_evidence_reference", f"Unknown evidence refs: {', '.join(invented)}."))
    if not packet.evidence and review.confidence > 0.7:
        findings.append(_warning("confidence_without_evidence", "High confidence is unsupported by supplied evidence."))
    if review.requires_human_approval:
        findings.append(_error("model_approval_forbidden", "GLM cannot decide approval requirements."))

    rendered = review.model_dump_json()
    if contains_secret(rendered):
        findings.append(_error("secret_in_result", "Result contains a secret-like value."))
    if artifact_version < 1:
        findings.append(_error("artifact_version_invalid", "Artifact version must be positive."))
    preflight = preflight_grant_review(normalized_source) if packet.task_type == "grant_review" else None
    schema_valid = bool(preflight.schema_valid) if preflight else True
    proposal_complete = bool(preflight.proposal_complete and all(proposal_fields_present.values())) if preflight else True
    eligibility_valid = bool(preflight.eligibility_valid) if preflight else True
    if preflight and preflight.mapping_errors:
        findings.append(_error("input_mapping_error", "Source draft was lost during input normalization."))
    return ValidationReport(
        ok=not any(item.severity == "error" for item in findings),
        task_id=packet.task_id,
        artifact_version=artifact_version,
        findings=findings,
        checked=checked,
        schema_valid=schema_valid,
        proposal_complete=proposal_complete,
        eligibility_valid=eligibility_valid,
    )


def build_candidate_artifact(
    packet: ContextPacket,
    review: GlmReview,
    validation: ValidationReport,
    *,
    artifact_version: int,
    source_payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    source = source_payload or {}
    if packet.task_type == "grant_review" and source:
        source, _ = normalize_grant_review(source)
    structured = {
        name: source[name]
        for name in (
            "age_range",
            "participant_count",
            "duration_minutes",
            "same_group",
            "free_event",
            "budget_total",
            "budget_items",
            "deadline",
            "event_window",
            "organization_status",
            "field_provenance",
        )
        if name in source
    }
    return {
        "schema_version": 1,
        "task_id": packet.task_id,
        "artifact_version": artifact_version,
        "draft": packet.draft,
        "requirements": packet.requirements,
        "evidence": [item.model_dump(mode="json") for item in packet.evidence],
        "evidence_count": len(packet.evidence),
        "budget_summary": packet.budget_summary,
        "known_gaps": packet.known_gaps,
        "requested_review": packet.requested_review,
        "structured_grant": structured,
        "glm_review": review.model_dump(mode="json"),
        "deterministic_validation": validation.model_dump(mode="json"),
        "schema_valid": validation.schema_valid,
        "proposal_complete": validation.proposal_complete,
        "eligibility_valid": validation.eligibility_valid,
        "application_mode": "consultative_suggestions_not_applied",
        "external_action_allowed": False,
    }


def _validate_budget(value: Any, findings: list[ValidationFinding]) -> None:
    if not isinstance(value, dict) or not value:
        findings.append(_warning("budget_missing", "Budget summary is missing."))
        return
    items = value.get("items") or value.get("lines")
    total = value.get("total")
    if not isinstance(items, list) or total is None:
        findings.append(_warning("budget_not_checkable", "Budget requires items/lines and total."))
        return
    try:
        expected = Decimal(str(total))
        actual = sum((Decimal(str(row.get("amount", 0))) for row in items if isinstance(row, dict)), Decimal("0"))
    except (InvalidOperation, ValueError):
        findings.append(_error("budget_type_invalid", "Budget contains a non-numeric amount."))
        return
    if abs(actual - expected) > Decimal("0.01"):
        findings.append(_error("budget_total_mismatch", f"Budget lines total {actual}, declared total {expected}."))


def _validate_dates(payload: dict[str, Any], findings: list[ValidationFinding]) -> None:
    raw = payload.get("deadline")
    if raw in (None, ""):
        findings.append(_warning("deadline_missing", "Grant deadline is missing."))
        return
    try:
        parsed = date.fromisoformat(str(raw)[:10])
    except ValueError:
        findings.append(_error("deadline_invalid", "Deadline is not ISO YYYY-MM-DD."))
        return
    if parsed < date.today():
        findings.append(_warning("deadline_passed", "Grant deadline is in the past."))


def _semantic_field_present(field: str, packet: ContextPacket) -> bool:
    markers = {
        "objectives": ("obiettiv", "objective", "risultato atteso", "outcome"),
        "activities": ("attivit", "activity", "incontr", "laborator", "session"),
        "kpis": ("kpi", "indicator", "partecipanti", "participants", "questionario", "output finale"),
    }[field]
    text = "\n".join(
        [
            packet.draft,
            *packet.requirements,
            *(f"{item.claim}\n{item.excerpt}" for item in packet.evidence),
        ]
    ).casefold()
    return any(marker in text for marker in markers)


def _keyword(value: str) -> str:
    words = [word for word in value.casefold().split() if len(word) >= 5]
    return words[0] if words else value.casefold()[:20]


def _error(code: str, message: str) -> ValidationFinding:
    return ValidationFinding(severity="error", code=code, message=message)


def _warning(code: str, message: str) -> ValidationFinding:
    return ValidationFinding(severity="warning", code=code, message=message)
