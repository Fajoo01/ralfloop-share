from __future__ import annotations

import hashlib
import json
import re
from typing import Annotated, Any, Literal, Mapping

from pydantic import BaseModel, ConfigDict, Field


class StrictDomainModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


DomainKey = Annotated[str, Field(min_length=1, max_length=160)]
DomainConstraint = Annotated[str, Field(min_length=1, max_length=400)]


class EmailEvidenceRef(StrictDomainModel):
    ref: str = Field(min_length=1, max_length=160)
    source: Literal["mail", "thread", "knowledge", "artifact", "user_intent", "policy"]
    excerpt: str = Field(min_length=1, max_length=1200)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class RequiredMeaning(StrictDomainModel):
    key: str = Field(min_length=1, max_length=96)
    text: str = Field(min_length=1, max_length=600)
    evidence_refs: list[DomainKey] = Field(min_length=1, max_length=8)
    deterministic_validator: str | None = Field(default=None, max_length=96)
    certainty: Literal["asserted", "uncertain"] = "asserted"


class SupportedFact(StrictDomainModel):
    key: str = Field(min_length=1, max_length=96)
    statement: str = Field(min_length=1, max_length=1200)
    evidence_refs: list[DomainKey] = Field(min_length=1, max_length=8)
    kind: Literal["fact", "temporal", "amount", "payment", "decision", "actor"] = "fact"
    certainty: Literal["asserted", "uncertain"] = "asserted"
    actor_refs: list[DomainKey] = Field(default_factory=list, max_length=4)


class DomainSubject(StrictDomainModel):
    name: str = Field(min_length=1, max_length=240)
    role: str = Field(min_length=1, max_length=80)
    evidence_refs: list[DomainKey] = Field(min_length=1, max_length=8)


class EmailReplyDomainV1(StrictDomainModel):
    schema_version: Literal["email_reply_domain_v1"] = "email_reply_domain_v1"
    required_meanings: list[RequiredMeaning] = Field(default_factory=list, max_length=32)
    supported_facts: list[SupportedFact] = Field(default_factory=list, max_length=64)
    unknown_facts: list[DomainKey] = Field(default_factory=list, max_length=32)
    forbidden_claims_without_evidence: list[DomainKey] = Field(default_factory=list, max_length=32)
    allowed_actions: list[DomainKey] = Field(default_factory=list, max_length=16)
    allowed_commitments: list[DomainKey] = Field(default_factory=list, max_length=16)
    forbidden_commitments_without_evidence: list[DomainKey] = Field(default_factory=list, max_length=16)
    supported_dates: list[SupportedFact] = Field(default_factory=list, max_length=32)
    supported_amounts: list[SupportedFact] = Field(default_factory=list, max_length=32)
    temporal_statements: list[SupportedFact] = Field(default_factory=list, max_length=32)
    payment_statements: list[SupportedFact] = Field(default_factory=list, max_length=32)
    decisions: list[SupportedFact] = Field(default_factory=list, max_length=16)
    subjects: list[DomainSubject] = Field(default_factory=list, max_length=16)
    user_constraints: list[DomainConstraint] = Field(default_factory=list, max_length=32)
    evidence: list[EmailEvidenceRef] = Field(default_factory=list, max_length=96)


class EmailReplyDomainValidationError(ValueError):
    def __init__(self, reason_code: str, *, domain_ref: str = "") -> None:
        self.reason_code = reason_code
        self.domain_ref = domain_ref
        super().__init__(reason_code)


_MEANING_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "confirm_documents_received",
        re.compile(
            r"(?:conferm|comunic|acknowledg).{0,100}(?:ricezion|ricevut|receipt).{0,80}(?:document|docs)|"
            r"(?:document|docs).{0,80}(?:ricevut|received).{0,80}(?:conferm|acknowledg)",
            re.I,
        ),
    ),
    (
        "proposal_review_next_week",
        re.compile(
            r"(?:propost|proposal).{0,120}(?:valutat|esaminat|review).{0,100}(?:settimana\s+prossima|next\s+week)|"
            r"(?:settimana\s+prossima|next\s+week).{0,100}(?:valutat|esaminat|review).{0,120}(?:propost|proposal)",
            re.I,
        ),
    ),
    ("participation_interest", re.compile(r"(?:potremmo|potrebbe|vorremmo|vorrebbe|desidereremmo|saremmo\s+interessat[ei]|sarebbe(?:ro)?\s+interessat[aei]).{0,100}partecip", re.I)),
    ("september_readiness_uncertain", re.compile(r"speriamo.{0,100}(?:pront[ei]|operativ[ei]|riuscire).{0,80}settembre", re.I)),
)

_MEANING_CONCEPTS: dict[str, tuple[re.Pattern[str], ...]] = {
    "confirm_documents_received": (
        re.compile(r"\b(?:document|docs)", re.I),
        re.compile(r"\b(?:ricezion|ricevut|received|receipt|acknowledg)", re.I),
    ),
    "proposal_review_next_week": (
        re.compile(r"\b(?:propost|proposal)", re.I),
        re.compile(r"\b(?:valutat|valuter|esaminat|esaminer|review)", re.I),
        re.compile(r"\b(?:settimana\s+prossima|prossima\s+settimana|next\s+week)", re.I),
    ),
    "participation_interest": (
        re.compile(r"\bpartecip", re.I),
        re.compile(r"\b(?:interessat|vorre|desider|piacere|potre)", re.I),
    ),
    "september_readiness_uncertain": (
        re.compile(r"\bsettembre\b", re.I),
        re.compile(r"\b(?:speriamo|probabilmente|potre|da\s+confermare|se\s+riusciremo)", re.I),
    ),
}

_FORBIDDEN_PATTERNS: dict[str, re.Pattern[str]] = {
    "documents_received": re.compile(
        r"(?:conferm|comunic).{0,100}(?:ricezion|ricevut).{0,80}document|"
        r"(?:abbiamo\s+)?ricevut.{0,80}document|"
        r"document.{0,80}(?:sono\s+stat[ei]\s+)?ricevut",
        re.I,
    ),
    "proposal_approved": re.compile(r"propost.{0,80}(?:[eè]\s+stata\s+|risulta\s+)?approvat", re.I),
    "contact_after_review": re.compile(
        r"(?:vi\s+)?(?:ri)?contatteremo|(?:vi\s+)?scriveremo|vi\s+faremo\s+sapere|"
        r"(?:dopo|al\s+termine\s+della).{0,80}valutazion.{0,80}(?:contatter|scriver)",
        re.I,
    ),
    "payment": re.compile(r"(?:pagheremo|effettueremo\s+(?:il\s+)?pagamento|provvederemo\s+al\s+pagamento)", re.I),
    "new_deadline": re.compile(r"(?:nuova|fissiamo|stabiliamo).{0,40}scadenz", re.I),
}

_DATE_RE = re.compile(
    r"\b(?:\d{1,2}[/-]\d{1,2}(?:[/-]\d{2,4})?|\d{1,2}\s+"
    r"(?:gennaio|febbraio|marzo|aprile|maggio|giugno|luglio|agosto|settembre|ottobre|novembre|dicembre)"
    r"(?:\s+\d{4})?)\b",
    re.I,
)
_AMOUNT_RE = re.compile(r"\b\d+(?:[.,]\d{1,2})?\s*(?:€|euro)\b", re.I)
_UNCERTAINTY_RE = re.compile(
    r"\b(?:probabilmente|forse|potre(?:bbe|mmo)|vorremmo|desidereremmo|saremmo|dovrebbe|da\s+confermare|"
    r"sarebbe(?:ro)?|ancora\s+da|non\s+(?:è|e|ha)\s+ancora|speriamo|likely|probably|may|might|to\s+be\s+confirmed)\b",
    re.I,
)
_TEMPORAL_STATEMENT_RE = re.compile(
    r"\b(?:settimana\s+prossima|next\s+week|(?:a|in|da)\s+settembre|"
    r"gennaio|febbraio|marzo|aprile|maggio|giugno|luglio|agosto|settembre|"
    r"ottobre|novembre|dicembre|scadenz|deadline|data|date|da\s+confermare)\b",
    re.I,
)
_PAYMENT_STATEMENT_RE = re.compile(
    r"\b(?:pagament|pagher|importo|contributo|costo|prezzo|euro|payment|amount|fee|cost)\b",
    re.I,
)
_ASSERTED_PARTICIPATION_RE = re.compile(
    r"\b(?:parteciperemo|confermiamo\s+(?:la\s+)?(?:nostra\s+)?partecipazione|"
    r"saremo\s+present[ei]|prenderemo\s+parte|essere\s+present[ei]|"
    r"(?:la\s+)?nostra\s+presenza)\b",
    re.I,
)


def build_email_reply_domain(context_packet: Mapping[str, Any]) -> EmailReplyDomainV1:
    """Build authoritative domain only from supplied evidence, intent and policy."""

    evidence: list[EmailEvidenceRef] = []
    facts: list[SupportedFact] = []
    required: list[RequiredMeaning] = []
    subjects: list[DomainSubject] = []
    dates: list[SupportedFact] = []
    amounts: list[SupportedFact] = []
    temporal_statements: list[SupportedFact] = []
    payment_statements: list[SupportedFact] = []
    decisions: list[SupportedFact] = []

    def add_evidence(ref: str, source: str, value: Any) -> str | None:
        excerpt = _compact_text(value, 1200)
        if not excerpt:
            return None
        if any(item.ref == ref for item in evidence):
            return ref
        evidence.append(EmailEvidenceRef(
            ref=ref,
            source=source,  # type: ignore[arg-type]
            excerpt=excerpt,
            sha256=hashlib.sha256(excerpt.encode("utf-8")).hexdigest(),
        ))
        return ref

    source = context_packet.get("source_email") if isinstance(context_packet.get("source_email"), Mapping) else {}
    body_ref = add_evidence("source_email.body", "mail", source.get("body"))
    subject_ref = add_evidence("source_email.subject", "mail", source.get("subject"))
    date_ref = add_evidence("source_email.date", "mail", source.get("date"))
    for ref in (body_ref, subject_ref, date_ref):
        if ref:
            item = next(value for value in evidence if value.ref == ref)
            facts.append(_fact("fact", item.excerpt, ref))

    thread = source.get("thread_context") if isinstance(source.get("thread_context"), list) else []
    for index, item in enumerate(thread[:24]):
        if not isinstance(item, Mapping):
            continue
        thread_sender = re.sub(r"\s*<[^>]+>\s*", "", str(item.get("sender") or "")).strip()
        sender_ref = add_evidence(f"source_email.thread_context[{index}].sender", "thread", thread_sender)
        if sender_ref:
            subjects.append(DomainSubject(name=thread_sender, role="thread_sender", evidence_refs=[sender_ref]))
        ref = add_evidence(f"source_email.thread_context[{index}]", "thread", item.get("body"))
        if ref:
            facts.append(_fact(
                "thread_fact", next(value.excerpt for value in evidence if value.ref == ref), ref,
                actor_refs=[sender_ref] if sender_ref else [],
            ))

    organization = context_packet.get("organization_context") if isinstance(context_packet.get("organization_context"), Mapping) else {}
    for index, value in enumerate((organization.get("relevant_facts") or [])[:32]):
        ref = add_evidence(f"organization_context.relevant_facts[{index}]", "knowledge", value)
        if ref:
            facts.append(_fact("organization_fact", next(item.excerpt for item in evidence if item.ref == ref), ref))

    artifacts = context_packet.get("structured_artifacts") if isinstance(context_packet.get("structured_artifacts"), list) else []
    for artifact_index, artifact in enumerate(artifacts[:8]):
        if not isinstance(artifact, Mapping) or artifact.get("content_role") != "data":
            continue
        artifact_facts = artifact.get("facts") if isinstance(artifact.get("facts"), list) else []
        for fact_index, value in enumerate(artifact_facts[:32]):
            if not isinstance(value, Mapping):
                continue
            statement = value.get("statement") or value.get("claim")
            if statement is None and value.get("field") is not None:
                statement = f"{value.get('field')}={value.get('value')}"
            ref = add_evidence(
                f"structured_artifacts[{artifact_index}].facts[{fact_index}]",
                "artifact",
                statement,
            )
            if ref:
                facts.append(_fact(
                    "artifact_fact", next(item.excerpt for item in evidence if item.ref == ref), ref,
                    kind="decision" if value.get("kind") == "decision" else "fact",
                ))

    signature = organization.get("signature") if isinstance(organization.get("signature"), Mapping) else {}
    signature_text = " | ".join(filter(None, (str(signature.get("name") or "").strip(), str(signature.get("organization") or "").strip())))
    signature_ref = add_evidence("organization_context.signature", "knowledge", signature_text)
    if signature_ref:
        if str(signature.get("name") or "").strip():
            subjects.append(DomainSubject(name=str(signature["name"]).strip(), role="authorized_signer", evidence_refs=[signature_ref]))
        if str(signature.get("organization") or "").strip():
            subjects.append(DomainSubject(name=str(signature["organization"]).strip(), role="replying_organization", evidence_refs=[signature_ref]))
    sender_name = re.sub(r"\s*<[^>]+>\s*", "", str(source.get("sender") or "")).strip()
    sender_ref = add_evidence("source_email.sender", "mail", sender_name)
    if sender_ref:
        subjects.append(DomainSubject(name=sender_name, role="source_sender", evidence_refs=[sender_ref]))

    intents = context_packet.get("user_intent") if isinstance(context_packet.get("user_intent"), list) else []
    for index, value in enumerate(intents[:32]):
        ref = add_evidence(f"user_intent[{index}]", "user_intent", value)
        if not ref:
            continue
        text = next(item.excerpt for item in evidence if item.ref == ref)
        matches = [(key, pattern) for key, pattern in _MEANING_RULES if pattern.search(text)]
        if matches:
            for key, _ in matches:
                required.append(RequiredMeaning(
                    key=key, text=text, evidence_refs=[ref], deterministic_validator=key,
                    certainty="uncertain" if _UNCERTAINTY_RE.search(text) else "asserted",
                ))
                # User-authored instruction is evidence for the narrowly scoped
                # acknowledgement it explicitly asks Ralf to preserve. It does
                # not authorize decisions, payments, dates, or open-ended facts.
                if key == "confirm_documents_received":
                    facts.append(_fact("user_asserted_fact", text, ref))
        else:
            key = "intent_" + hashlib.sha256(text.casefold().encode()).hexdigest()[:16]
            required.append(RequiredMeaning(
                key=key, text=text, evidence_refs=[ref], deterministic_validator=None,
                certainty="uncertain" if _UNCERTAINTY_RE.search(text) else "asserted",
            ))

    constraints = context_packet.get("reply_constraints") if isinstance(context_packet.get("reply_constraints"), Mapping) else {}
    user_constraints: list[str] = []
    for key, value in sorted(constraints.items()):
        if value is True or isinstance(value, str) and value.strip():
            text = _compact_text(f"{key}={str(value).lower() if isinstance(value, bool) else value}", 400)
            user_constraints.append(text)
            add_evidence(f"reply_constraints.{key}", "policy", text)

    all_evidence_text = "\n".join(item.excerpt for item in evidence)
    fact_evidence_text = "\n".join(
        item.excerpt for item in evidence if item.source in {"mail", "thread", "knowledge", "artifact"}
    )
    for item in evidence:
        for token in _DATE_RE.findall(item.excerpt):
            dates.append(_fact("date", token, item.ref, kind="temporal"))
        for token in _AMOUNT_RE.findall(item.excerpt):
            amounts.append(_fact("amount", token, item.ref, kind="amount"))
        if item.source in {"mail", "thread", "knowledge", "artifact", "user_intent"} and _TEMPORAL_STATEMENT_RE.search(item.excerpt):
            temporal_statements.append(_fact("temporal", item.excerpt, item.ref, kind="temporal"))
        if item.source in {"mail", "thread", "knowledge", "artifact", "user_intent"} and _PAYMENT_STATEMENT_RE.search(item.excerpt):
            payment_statements.append(_fact("payment", item.excerpt, item.ref, kind="payment"))
        if item.source in {"mail", "thread", "knowledge", "artifact"} and re.search(
            r"\b(?:approvat[aoei]|accettat[aoei]|autorizzat[aoei])\b", item.excerpt, re.I
        ):
            decisions.append(_fact("decision", item.excerpt, item.ref, kind="decision"))

    allowed_commitments: list[str] = []
    if any(item.key == "proposal_review_next_week" for item in required):
        allowed_commitments.append("proposal_review_next_week")
    if re.search(r"(?:contatteremo|scriveremo|vi\s+faremo\s+sapere).{0,100}(?:valutazion|review)", all_evidence_text, re.I):
        allowed_commitments.append("contact_after_review")
    if re.search(r"(?:pagheremo|effettueremo\s+(?:il\s+)?pagamento)", all_evidence_text, re.I):
        allowed_commitments.append("payment")

    supported_claims = set()
    if _FORBIDDEN_PATTERNS["documents_received"].search(fact_evidence_text):
        supported_claims.add("documents_received")
    if any(item.key == "confirm_documents_received" for item in required):
        supported_claims.add("documents_received")
    if decisions:
        supported_claims.add("proposal_approved")
    forbidden = [
        key for key in _FORBIDDEN_PATTERNS
        if key not in allowed_commitments and key not in supported_claims
    ]
    return EmailReplyDomainV1(
        required_meanings=_dedupe_models(required, "key"),
        supported_facts=_dedupe_models(facts, "key"),
        unknown_facts=list(forbidden),
        forbidden_claims_without_evidence=list(forbidden),
        allowed_actions=["acknowledge", "inform", "reply_draft"],
        allowed_commitments=sorted(set(allowed_commitments)),
        forbidden_commitments_without_evidence=[key for key in ("contact_after_review", "payment") if key in forbidden],
        supported_dates=_dedupe_models(dates, "key"),
        supported_amounts=_dedupe_models(amounts, "key"),
        temporal_statements=_dedupe_models(temporal_statements, "key"),
        payment_statements=_dedupe_models(payment_statements, "key"),
        decisions=_dedupe_models(decisions, "key"),
        subjects=_dedupe_models(subjects, "name", secondary="role"),
        user_constraints=user_constraints,
        evidence=evidence,
    )


def validate_draft_against_domain(body: str, domain: EmailReplyDomainV1 | Mapping[str, Any]) -> str:
    value = domain if isinstance(domain, EmailReplyDomainV1) else EmailReplyDomainV1.model_validate(domain)
    folded = _compact_text(body, 20_000).casefold()
    for meaning in value.required_meanings:
        if not meaning.deterministic_validator:
            continue
        concepts = _MEANING_CONCEPTS.get(meaning.deterministic_validator)
        if concepts is not None and not all(pattern.search(folded) for pattern in concepts):
            raise EmailReplyDomainValidationError("draft_domain_missing_required_meaning", domain_ref=meaning.key)

    uncertain_participation = any(
        meaning.key == "participation_interest" and meaning.certainty == "uncertain"
        for meaning in value.required_meanings
    )
    asserted_participation_evidence = any(
        fact.certainty == "asserted"
        and all(pattern.search(fact.statement) for pattern in _MEANING_CONCEPTS["participation_interest"])
        and (
            any(ref.startswith("organization_context.relevant_facts[") for ref in fact.evidence_refs)
            or fact.kind == "decision" and any(
                ref.startswith("structured_artifacts[") for ref in fact.evidence_refs
            )
        )
        for fact in value.supported_facts
    )
    if uncertain_participation and not asserted_participation_evidence and (
        re.search(r"\b(?:siamo|sono|[eè])\s+interessat[aeio]\b", folded, re.I)
        or re.search(r"\b(?:parteciperemo|parteciper[aà]|garantiamo|confermiamo\s+la\s+partecipazione)\b", folded, re.I)
        or _ASSERTED_PARTICIPATION_RE.search(folded)
    ):
        raise EmailReplyDomainValidationError(
            "draft_domain_strengthens_uncertain_meaning", domain_ref="participation_interest"
        )

    for key in value.forbidden_claims_without_evidence:
        pattern = _FORBIDDEN_PATTERNS.get(key)
        if pattern is not None and pattern.search(folded):
            raise EmailReplyDomainValidationError("draft_domain_forbidden_claim", domain_ref=key)

    supported_dates = {_normalize_token(item.statement) for item in value.supported_dates}
    for token in _DATE_RE.findall(body):
        if _normalize_token(token) not in supported_dates:
            raise EmailReplyDomainValidationError("draft_domain_unsupported_date", domain_ref=token)
    supported_amounts = {_normalize_token(item.statement) for item in value.supported_amounts}
    for token in _AMOUNT_RE.findall(body):
        if _normalize_token(token) not in supported_amounts:
            raise EmailReplyDomainValidationError("draft_domain_unsupported_amount", domain_ref=token)
    return "passed"


def _fact(
    prefix: str,
    statement: str,
    evidence_ref: str,
    *,
    kind: Literal["fact", "temporal", "amount", "payment", "decision", "actor"] = "fact",
    actor_refs: list[str] | None = None,
) -> SupportedFact:
    normalized = _normalize_token(statement)
    return SupportedFact(
        key=f"{prefix}_{hashlib.sha256(normalized.encode()).hexdigest()[:16]}",
        statement=_compact_text(statement, 1200),
        evidence_refs=[evidence_ref],
        kind=kind,
        certainty="uncertain" if _UNCERTAINTY_RE.search(statement) else "asserted",
        actor_refs=actor_refs or [],
    )


def _compact_text(value: Any, maximum: int) -> str:
    return " ".join(str(value or "").split())[:maximum].strip()


def _normalize_token(value: str) -> str:
    return " ".join(value.casefold().replace(",", ".").split())


def _dedupe_models(values: list[Any], primary: str, *, secondary: str | None = None) -> list[Any]:
    output: list[Any] = []
    seen: set[str] = set()
    for value in values:
        key = str(getattr(value, primary))
        if secondary:
            key += "\x00" + str(getattr(value, secondary))
        if key not in seen:
            seen.add(key)
            output.append(value)
    return output


def domain_digest(domain: EmailReplyDomainV1 | Mapping[str, Any]) -> str:
    value = domain if isinstance(domain, EmailReplyDomainV1) else EmailReplyDomainV1.model_validate(domain)
    raw = json.dumps(value.model_dump(mode="json"), ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(raw).hexdigest()


__all__ = [
    "EmailEvidenceRef",
    "EmailReplyDomainV1",
    "EmailReplyDomainValidationError",
    "RequiredMeaning",
    "SupportedFact",
    "build_email_reply_domain",
    "domain_digest",
    "validate_draft_against_domain",
]
