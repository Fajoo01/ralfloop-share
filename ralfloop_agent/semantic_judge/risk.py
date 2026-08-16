from __future__ import annotations

import re
from typing import Annotated, Any, Literal, Mapping

from pydantic import BaseModel, ConfigDict, Field

from ralfloop_agent.domains.email_reply import EmailReplyDomainV1, build_email_reply_domain


RiskReason = Annotated[str, Field(min_length=1, max_length=96)]


class EmailRiskAssessment(BaseModel):
    """Deterministic, explainable routing result for the semantic critic."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    level: Literal["low", "normal", "high"]
    reasons: list[RiskReason] = Field(min_length=1, max_length=16)


_MONEY_RE = re.compile(
    r"\b(?:euro|€|import[oi]|pagament[oi]|pagare|pagher|costo|prezzo|quota|rimborso)\b",
    re.I,
)
_FUNDING_RE = re.compile(
    r"\b(?:band[oi]|contribut[oi]|finanziament[oi]|sovvenzion[ei]|grant|funding|candidatur[ae])\b",
    re.I,
)
_DEADLINE_RE = re.compile(r"\b(?:scadenz[ae]|termine ultimo|entro il|deadline)\b", re.I)
_DECISION_RE = re.compile(
    r"\b(?:approvat[aoei]|approvazion[ei]|autorizzat[aoei]|decision[ei]|accettat[aoei]|respint[aoei])\b",
    re.I,
)
_COMMITMENT_RE = re.compile(
    r"\b(?:impegn[oi]|ci impegniamo|garantiamo|confermiamo la partecipazione|parteciperemo|"
    r"pagheremo|firmeremo|stipuleremo|vi (?:ri)?contatteremo|vi scriveremo|"
    r"vi comunicheremo|vi faremo sapere|we will|we commit|we guarantee)\b",
    re.I,
)
_PARTNER_RE = re.compile(r"\b(?:partner|partnership|accord[oi]|intesa|convenzion[ei])\b", re.I)
_REPUTATIONAL_RE = re.compile(
    r"\b(?:dichiarazione ufficiale|comunicato stampa|legale|avvocat[oa]|contratt[oi]|"
    r"vincolante|reputazional[ei]|pubblicazione ufficiale)\b",
    re.I,
)
_ROUTINE_RE = re.compile(
    r"\b(?:grazie|ringrazi|ricevut[oaie]|ricezione|presa visione|acknowledg|thanks|received)\b",
    re.I,
)
_SUBSTANTIVE_RE = re.compile(
    r"\b(?:proposta|progetto|attivit[àa]|partecip|richiest|valut|organizz|evento|"
    r"documentazione|informazion|disponibilit[àa])\b",
    re.I,
)
_UNCERTAIN_ROLE_RE = re.compile(
    r"\b(?:capire|definire|chiarire|valutare)\b.{0,100}\b(?:ruolo|modo|modalit[àa]|contributo)\b|"
    r"\b(?:ruolo|modo|modalit[àa]|contributo)\b.{0,100}\b(?:da\s+definire|da\s+chiarire|"
    r"non\s+(?:[eè]|risulta)\s+(?:ancora\s+)?chiar[oa]|ancora\s+da)\b",
    re.I,
)


def assess_email_risk(
    context_packet: Mapping[str, Any],
    draft: str = "",
    *,
    classification: str | None = None,
) -> EmailRiskAssessment:
    """Route from structured evidence/domain; never calls a model or mutates input."""

    override = classification or _structured_override(context_packet)
    if override:
        level = _normalize_level(override)
        return EmailRiskAssessment(level=level, reasons=[f"explicit_override:{level}"])

    raw_domain = context_packet.get("email_reply_domain_v1")
    domain = (
        EmailReplyDomainV1.model_validate(raw_domain)
        if isinstance(raw_domain, Mapping)
        else build_email_reply_domain(context_packet)
    )
    semantic_text = _semantic_text(context_packet, draft)
    reasons: list[str] = []

    if domain.supported_amounts or domain.payment_statements or _MONEY_RE.search(semantic_text):
        reasons.append("money_or_payment")
    if _FUNDING_RE.search(semantic_text):
        reasons.append("grant_or_funding")
    if _DEADLINE_RE.search(semantic_text):
        reasons.append("deadline")
    if domain.decisions or _DECISION_RE.search(semantic_text):
        reasons.append("decision_or_approval")

    non_routine_commitments = {
        value for value in domain.allowed_commitments
        if value not in {"proposal_review_next_week"}
    }
    if non_routine_commitments or _COMMITMENT_RE.search(semantic_text):
        reasons.append("association_commitment")
    if _PARTNER_RE.search(semantic_text):
        reasons.append("partner_agreement")
    if re.search(
        r"\b(?:vi (?:ri)?contatteremo|vi scriveremo|vi comunicheremo|vi faremo sapere|"
        r"garantiamo|certamente|sicuramente)\b",
        semantic_text,
        re.I,
    ):
        reasons.append("expectation_creating_language")

    external_subjects = {
        subject.name.casefold()
        for subject in domain.subjects
        if subject.role in {"thread_sender", "source_sender"} and subject.name.strip()
    }
    actor_refs = {
        ref
        for fact in domain.supported_facts
        for ref in fact.actor_refs
        if ref
    }
    if len(external_subjects) > 1 or len(actor_refs) > 1:
        reasons.append("multiple_attributed_subjects")

    uncertain_facts = [
        item for item in (*domain.supported_facts, *domain.required_meanings)
        if item.certainty == "uncertain"
    ]
    uncertain_temporal = [
        item for item in (*domain.temporal_statements, *domain.supported_dates)
        if item.certainty == "uncertain"
    ]
    if uncertain_facts:
        reasons.append("uncertain_fact")
    if uncertain_temporal:
        reasons.append("temporal_uncertainty")
    constraints = "\n".join(domain.user_constraints).casefold()
    if uncertain_facts or "preserve_uncertainty=true" in constraints:
        reasons.append("meaning_strengthening_risk")
    if _UNCERTAIN_ROLE_RE.search(semantic_text):
        reasons.append("uncertain_participation_role")
    if _REPUTATIONAL_RE.search(semantic_text):
        reasons.append("binding_or_reputational")

    reasons = _unique(reasons)
    if reasons:
        return EmailRiskAssessment(level="high", reasons=reasons[:16])

    intent_text = " ".join(str(item) for item in (context_packet.get("user_intent") or []))
    if _ROUTINE_RE.search(intent_text + "\n" + draft) and not _SUBSTANTIVE_RE.search(intent_text):
        return EmailRiskAssessment(level="low", reasons=["routine_acknowledgement"])
    return EmailRiskAssessment(level="normal", reasons=["default_business_reply"])


def _structured_override(packet: Mapping[str, Any]) -> str | None:
    constraints = packet.get("reply_constraints")
    if not isinstance(constraints, Mapping):
        return None
    for key in ("semantic_risk_override", "semantic_risk_level", "risk_classification"):
        value = constraints.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return None


def _normalize_level(value: str) -> Literal["low", "normal", "high"]:
    normalized = value.strip().casefold()
    if normalized not in {"low", "normal", "high"}:
        raise ValueError("semantic_risk_override_invalid")
    return normalized  # type: ignore[return-value]


def _semantic_text(packet: Mapping[str, Any], draft: str) -> str:
    values: list[str] = [draft]
    source = packet.get("source_email")
    if isinstance(source, Mapping):
        values.extend(str(source.get(key) or "") for key in ("subject", "body"))
        thread = source.get("thread_context")
        if isinstance(thread, list):
            for item in thread[:24]:
                if isinstance(item, Mapping):
                    values.extend((str(item.get("sender") or ""), str(item.get("body") or "")))
    organization = packet.get("organization_context")
    if isinstance(organization, Mapping):
        values.extend(str(item) for item in (organization.get("relevant_facts") or [])[:32])
    values.extend(str(item) for item in (packet.get("user_intent") or [])[:32])
    return "\n".join(values)


def _unique(values: list[str]) -> list[str]:
    output: list[str] = []
    seen: set[str] = set()
    for value in values:
        if value not in seen:
            seen.add(value)
            output.append(value)
    return output


__all__ = ["EmailRiskAssessment", "assess_email_risk"]
