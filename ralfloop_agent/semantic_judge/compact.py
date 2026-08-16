from __future__ import annotations

from dataclasses import dataclass
import json
import re
from typing import Any, Literal, Mapping

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator


CompactIssueCode = Literal["UC", "MC", "CT", "MR", "DT", "AM", "ID", "UF", "OE", "TC", "SI"]

ISSUE_TYPE_BY_CODE: dict[str, str] = {
    "UC": "unsupported_commitment",
    "MC": "meaning_changed",
    "CT": "contradiction",
    "MR": "missing_required_meaning",
    "DT": "invented_date",
    "AM": "invented_amount",
    "ID": "invented_decision",
    "UF": "unsupported_claim",
    "OE": "overstated_evidence",
    "TC": "tone_changes_meaning",
    "SI": "other_semantic_incongruity",
}

_REASON_BY_CODE = {
    "UC": "The clause adds a commitment not authorized by the domain.",
    "MC": "The clause changes the meaning authorized by the domain.",
    "CT": "The clause contradicts the referenced domain evidence.",
    "MR": "The draft omits a meaning required by the domain.",
    "DT": "The clause adds a date not supported by the domain.",
    "AM": "The clause adds an amount or payment not supported by the domain.",
    "ID": "The clause adds a decision not supported by the domain.",
    "UF": "The clause states a fact not supported by the domain.",
    "OE": "The clause is stronger than the referenced evidence.",
    "TC": "The wording changes the meaning authorized by the domain.",
    "SI": "The clause is semantically inconsistent with the domain.",
}


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CompactSemanticPatch(_StrictModel):
    v: Literal["P", "R"]
    t: CompactIssueCode | None = None
    d: int | None = Field(default=None, ge=-1, le=63)
    e: int | None = Field(default=None, ge=0, le=127)

    @model_validator(mode="after")
    def coherent(self) -> "CompactSemanticPatch":
        if self.v == "P" and any(value is not None for value in (self.t, self.d, self.e)):
            raise ValueError("compact_pass_has_issue")
        if self.v == "R" and any(value is None for value in (self.t, self.d, self.e)):
            raise ValueError("compact_repair_missing_reference")
        if self.v == "R" and (self.t == "MR") != (self.d == -1):
            raise ValueError("compact_missing_meaning_draft_reference_invalid")
        return self


@dataclass(frozen=True)
class CompactDomainEntry:
    kind: Literal["R", "F", "X", "C"]
    certainty: Literal["A", "?"]
    text: str
    domain_ref: str

    def wire(self, index: int) -> list[Any]:
        return [index, self.kind + self.certainty, self.text]


@dataclass(frozen=True)
class CompactCriticContext:
    prompt: str
    entries: tuple[CompactDomainEntry, ...]
    draft_clauses: tuple[str, ...]

    @property
    def dynamic_packet(self) -> dict[str, list[Any]]:
        return {
            "E": [entry.wire(index) for index, entry in enumerate(self.entries)],
            "D": [[index, clause] for index, clause in enumerate(self.draft_clauses)],
        }


_STATIC_PROMPT = (
    "Email semantic critic. Judge meaning, not wording: faithful paraphrases are valid. "
    "E=[id,code,text], D=[id,clause]; copy printed ids. E authoritative. "
    "R=required,F=fact,X=forbidden,C=allowed; A=certain,?=uncertain. P iff every R meaning is present "
    "and D adds, contradicts, or strengthens nothing. Reply ONLY {\"v\":\"P\"} or "
    "{\"v\":\"R\",\"t\":\"CODE\",\"d\":id,\"e\":id}; minified JSON, no whitespace. "
    "CODE must be one listed below, never CODE. "
    "One worst. MR only absent R and d=-1. "
    "Check R first: any absent R => MR with its id. Then X: future act/promise/contact in X => UC; "
    "never MC for X. T:CT opposite F/wrong actor; MC changed meaning; "
    "OE stronger ?; DT date; AM amount/payment; ID decision; UF fact; TC tone; SI other."
)


def compact_critic_context(context_packet: Mapping[str, Any], draft: str) -> CompactCriticContext:
    domain = context_packet.get("email_reply_domain_v1")
    if not isinstance(domain, Mapping):
        raise ValueError("semantic_judge_domain_missing")
    entries_list = _domain_entries(domain)
    missing_ref = _deterministic_missing_ref(domain, draft)
    if missing_ref:
        entries_list.sort(key=lambda item: item.domain_ref != missing_ref)
    entries = tuple(entries_list)
    clauses = tuple(_draft_clauses(draft))
    if not clauses:
        clauses = ("",)
    packet = {
        "E": [entry.wire(index) for index, entry in enumerate(entries)],
        "D": [[index, clause] for index, clause in enumerate(clauses)],
    }
    prompt = _STATIC_PROMPT + "\nDATA=" + json.dumps(packet, ensure_ascii=False, separators=(",", ":"))
    return CompactCriticContext(prompt=prompt, entries=entries, draft_clauses=clauses)


def parse_compact_patch(
    raw: bytes | str,
    context: CompactCriticContext,
    *,
    max_bytes: int = 1_024,
) -> CompactSemanticPatch:
    data = raw if isinstance(raw, bytes) else raw.encode("utf-8")
    if len(data) > max_bytes:
        raise ValueError("compact_semantic_output_too_long")
    try:
        text = data.decode("utf-8", errors="strict")
        if not text.strip():
            raise ValueError("compact_semantic_empty_output")
        value = json.loads(text.strip())
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("compact_semantic_malformed_json") from exc
    if not isinstance(value, dict):
        raise ValueError("compact_semantic_not_object")
    expected = {"v"} if value.get("v") == "P" else {"v", "t", "d", "e"}
    if set(value) != expected:
        raise ValueError("compact_semantic_schema_invalid")
    try:
        patch = CompactSemanticPatch.model_validate(value)
    except ValidationError as exc:
        raise ValueError("compact_semantic_schema_invalid") from exc
    if patch.v == "R":
        assert patch.d is not None and patch.e is not None
        if patch.d >= len(context.draft_clauses) or patch.e >= len(context.entries):
            raise ValueError("compact_semantic_reference_out_of_range")
    return patch


def expand_compact_patch(patch: CompactSemanticPatch, context: CompactCriticContext) -> Any:
    from ralfloop_agent.semantic_judge.core import SemanticIssue, SemanticReview

    if patch.v == "P":
        return SemanticReview(verdict="pass", issues=[], summary="")
    assert patch.t is not None and patch.d is not None and patch.e is not None
    entry = context.entries[patch.e]
    draft_text = "" if patch.d == -1 else context.draft_clauses[patch.d][:320]
    issue = SemanticIssue(
        type=ISSUE_TYPE_BY_CODE[patch.t],
        severity="high",
        draft_text=draft_text,
        reason=_REASON_BY_CODE[patch.t],
        domain_refs=[entry.domain_ref],
    )
    return SemanticReview(verdict="repair", issues=[issue], summary="")


def _domain_entries(domain: Mapping[str, Any]) -> list[CompactDomainEntry]:
    output: list[CompactDomainEntry] = []
    seen: set[tuple[str, str, str]] = set()
    subjects = {
        str(ref): str(item.get("name") or "")
        for item in domain.get("subjects") or [] if isinstance(item, Mapping)
        for ref in item.get("evidence_refs") or []
    }

    def add(kind: Literal["R", "F", "X", "C"], certainty: str, text: Any, ref: Any) -> None:
        compact = " ".join(str(text or "").split())[:240]
        domain_ref = " ".join(str(ref or "").split())[:160]
        key = (kind, certainty, compact.casefold())
        if compact and domain_ref and key not in seen:
            seen.add(key)
            output.append(CompactDomainEntry(kind, "?" if certainty == "uncertain" else "A", compact, domain_ref))

    for item in domain.get("required_meanings") or []:
        if isinstance(item, Mapping):
            canonical = item.get("deterministic_validator") or item.get("text") or item.get("key")
            add("R", str(item.get("certainty")), str(canonical).replace("_", " "), item.get("key"))
    for item in domain.get("supported_facts") or []:
        if not isinstance(item, Mapping):
            continue
        refs = [str(ref) for ref in item.get("evidence_refs") or []]
        if refs and all(ref.endswith((".subject", ".date", ".sender")) for ref in refs):
            continue
        actors = [subjects.get(str(ref), "") for ref in item.get("actor_refs") or []]
        statement = str(item.get("statement") or item.get("key") or "")
        if any(actors):
            statement = "/".join(actor for actor in actors if actor) + ": " + statement
        add("F", str(item.get("certainty")), statement, item.get("key"))
    for ref in domain.get("forbidden_claims_without_evidence") or []:
        display = {
            "contact_after_review": "promise to contact/notify after review",
            "payment": "promise payment",
            "proposal_approved": "proposal approved",
            "new_deadline": "new deadline",
        }.get(str(ref), str(ref).replace("_", " "))
        add("X", "asserted", display, ref)
    for ref in domain.get("allowed_commitments") or []:
        add("C", "asserted", str(ref).replace("_", " "), ref)
    if not domain.get("supported_dates"):
        add("X", "asserted", "exact date", "unsupported_date")
    if not domain.get("supported_amounts"):
        add("X", "asserted", "amount/payment", "unsupported_amount")
    return output[:128]


def _draft_clauses(draft: str) -> list[str]:
    parts = re.split(r"(?<=[.!?])\s+|\s*[;:]\s*|\s+(?:e|ma|mentre)\s+", " ".join(draft.split()), flags=re.I)
    return [part.strip(" \t\r\n,;:.!?")[:320] for part in parts if part.strip(" \t\r\n,;:.!?")][:64]


def _deterministic_missing_ref(domain: Mapping[str, Any], draft: str) -> str:
    from ralfloop_agent.domains.email_reply import (
        EmailReplyDomainValidationError,
        validate_draft_against_domain,
    )

    try:
        validate_draft_against_domain(draft, domain)
    except EmailReplyDomainValidationError as exc:
        if exc.reason_code == "draft_domain_missing_required_meaning":
            return exc.domain_ref
    return ""


__all__ = [
    "CompactCriticContext", "CompactDomainEntry", "CompactIssueCode", "CompactSemanticPatch",
    "ISSUE_TYPE_BY_CODE", "compact_critic_context", "expand_compact_patch", "parse_compact_patch",
]
