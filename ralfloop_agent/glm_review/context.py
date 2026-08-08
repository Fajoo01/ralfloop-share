from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Iterable

from ralfloop_agent.domains.bandi_semantic_retrieval import make_evidence, retrieve_for_bandi

from .models import ContextPacket, EvidenceItem


SECRET_PATTERNS = (
    re.compile(r"(?i)\b(api[_-]?key|access[_-]?token|refresh[_-]?token|password|passwd|secret|cookie)\b\s*[:=]\s*([^\s,;]+)"),
    re.compile(r"\b(?:ghp|github_pat|sk)-[A-Za-z0-9_-]{12,}\b"),
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/-]{12,}=*"),
)


def redact_secrets(value: str) -> tuple[str, int]:
    text = str(value)
    count = 0
    for pattern in SECRET_PATTERNS:
        def replace(match: re.Match[str]) -> str:
            nonlocal count
            count += 1
            if match.lastindex and match.lastindex >= 2:
                return f"{match.group(1)}=<redacted>"
            return "<redacted>"

        text = pattern.sub(replace, text)
    return text, count


def contains_secret(value: str) -> bool:
    for pattern in SECRET_PATTERNS:
        for match in pattern.finditer(value):
            if match.lastindex and match.lastindex >= 2 and "redacted" in match.group(2).casefold():
                continue
            return True
    return False


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def packet_hash(packet: ContextPacket) -> str:
    return hashlib.sha256(canonical_json(packet.model_dump(mode="json")).encode("utf-8")).hexdigest()


def build_context_packet(
    payload: dict[str, Any],
    *,
    task_id: str,
    task_type: str,
    max_chars: int = 10_000,
    top_k: int = 8,
) -> tuple[ContextPacket, dict[str, Any]]:
    if max_chars < 2_000:
        raise ValueError("context_limit_too_small")
    redactions = 0

    def clean(value: Any, limit: int) -> str:
        nonlocal redactions
        redacted, found = redact_secrets(str(value or ""))
        redactions += found
        return " ".join(redacted.split())[:limit]

    def clean_verbatim(value: Any, limit: int) -> str:
        nonlocal redactions
        redacted, found = redact_secrets(str(value or ""))
        redactions += found
        return redacted[:limit]

    goal = clean(payload.get("goal") or _default_goal(task_type), 2_000)
    constraints = _clean_list(payload.get("constraints"), clean, 500)
    requirements = _clean_list(payload.get("requirements"), clean, 700)
    known_gaps = _clean_list(payload.get("known_gaps"), clean, 700)
    requested_review = _clean_list(
        payload.get("requested_review") or _default_requested_review(task_type), clean, 500
    )
    evidence = _rank_and_normalize_evidence(
        payload.get("evidence") or [],
        query=" ".join([goal, *requirements]),
        clean=clean,
        top_k=top_k,
    )
    draft = clean_verbatim(payload.get("draft"), 12_000)
    budget = _redact_tree(payload.get("budget_summary") or {}, clean)

    packet = ContextPacket(
        task_id=task_id,
        task_type=task_type,
        goal=goal,
        constraints=constraints,
        draft=draft,
        requirements=requirements,
        evidence=evidence,
        budget_summary=budget if isinstance(budget, dict) else {},
        known_gaps=known_gaps,
        requested_review=requested_review,
    )
    packet = _fit_packet(packet, max_chars=max_chars)
    encoded = canonical_json(packet.model_dump(mode="json"))
    return packet, {
        "sha256": hashlib.sha256(encoded.encode("utf-8")).hexdigest(),
        "characters": len(encoded),
        "redactions": redactions,
        "evidence_count": len(packet.evidence),
    }


def _rank_and_normalize_evidence(
    values: Iterable[Any],
    *,
    query: str,
    clean: Any,
    top_k: int,
) -> list[EvidenceItem]:
    unique: list[EvidenceItem] = []
    seen: set[str] = set()
    for index, raw in enumerate(values):
        if not isinstance(raw, dict):
            continue
        source_id = clean(raw.get("source_id") or raw.get("chunk_id") or f"source-{index + 1}", 200)
        location = clean(
            raw.get("location")
            or raw.get("canonical_url")
            or raw.get("document_id")
            or raw.get("path")
            or "unknown",
            500,
        )
        excerpt = clean(raw.get("excerpt") or raw.get("text"), 1_200)
        claim = clean(raw.get("claim") or excerpt[:400] or "source evidence", 1_000)
        classification = str(raw.get("classification") or "fact")
        if classification not in {"fact", "hypothesis", "missing_data"}:
            classification = "fact"
        if not excerpt:
            continue
        key = hashlib.sha256(f"{source_id}\0{location}\0{claim}\0{excerpt}".casefold().encode()).hexdigest()
        if key in seen:
            continue
        seen.add(key)
        unique.append(
            EvidenceItem(
                source_id=source_id,
                location=location,
                claim=claim,
                excerpt=excerpt,
                classification=classification,
            )
        )
    if len(unique) <= top_k:
        return unique
    envelopes = [
        make_evidence(
            source_id=item.source_id,
            document_id=f"glm-{index}",
            canonical_url_value=_safe_url(item.location, index),
            source_type="attachment",
            text=f"{item.claim}\n{item.excerpt}",
            ordinal=index,
        )
        for index, item in enumerate(unique)
    ]
    ranked = retrieve_for_bandi(
        query,
        envelopes,
        semantic_invoke=None,
        top_k=top_k,
        minimum_documents=0,
        minimum_tokens=0,
    )
    by_source = {item.source_id: item for item in unique}
    return [by_source[item.source_id] for item in ranked.evidence if item.source_id in by_source]


def _fit_packet(packet: ContextPacket, *, max_chars: int) -> ContextPacket:
    data = packet.model_dump(mode="json")
    while len(canonical_json(data)) > max_chars and data["evidence"]:
        data["evidence"].pop()
    if len(canonical_json(data)) > max_chars:
        excess = len(canonical_json(data)) - max_chars
        data["draft"] = data["draft"][: max(0, len(data["draft"]) - excess - 64)]
    while len(canonical_json(data)) > max_chars and data["requirements"]:
        data["requirements"].pop()
    while len(canonical_json(data)) > max_chars and data["constraints"]:
        data["constraints"].pop()
    if len(canonical_json(data)) > max_chars:
        raise ValueError("context_packet_cannot_fit")
    return ContextPacket.model_validate(data)


def _clean_list(value: Any, clean: Any, item_limit: int) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for item in (clean(raw, item_limit) for raw in value) if item]


def _redact_tree(value: Any, clean: Any) -> Any:
    if isinstance(value, dict):
        return {clean(key, 200): _redact_tree(item, clean) for key, item in list(value.items())[:100]}
    if isinstance(value, list):
        return [_redact_tree(item, clean) for item in value[:100]]
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return clean(value, 700)


def _safe_url(location: str, index: int) -> str:
    if location.startswith(("https://", "http://")):
        return location
    digest = hashlib.sha256(location.encode()).hexdigest()[:16]
    return f"https://local.invalid/evidence/{index}-{digest}"


def _default_goal(task_type: str) -> str:
    if task_type == "grant_review":
        return "Review the grant draft for coherence, compliance, evidence gaps, KPI and budget risks."
    if task_type == "technical_review":
        return "Review the technical draft for contradictions, missing evidence, safety and operational risks."
    return "Review the draft critically and propose evidence-grounded improvements."


def _default_requested_review(task_type: str) -> list[str]:
    common = ["contradictions", "missing evidence", "unsupported claims", "risk flags"]
    if task_type == "grant_review":
        return [*common, "requirements coverage", "objectives activities KPI coherence", "budget coherence"]
    return common
