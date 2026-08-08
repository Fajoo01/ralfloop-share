from __future__ import annotations

from collections import Counter
from dataclasses import asdict, dataclass, field, replace
import hashlib
import math
import re
from typing import Any, Callable, Iterable
import urllib.parse

from bs4 import BeautifulSoup


SOURCE_TYPES = {"web", "pdf", "faq", "attachment", "regulation"}
CRITICAL_DOCUMENT_TYPES = {
    "official_call_text",
    "eligibility_requirements",
    "admissibility_criteria",
    "financial_plan",
    "official_amendment",
}
CRITICAL_CLAIMS = {
    "eligibility",
    "deadline",
    "territory",
    "funding_min",
    "funding_max",
    "cofunding",
    "partnership_required",
    "RUNTS_requirement",
    "project_duration",
    "excluded_expenses",
}
CLAIM_TERMS = {
    "eligibility": ("beneficiari", "chi può partecipare", "soggetti ammessi", "aps", "ets"),
    "deadline": ("scadenza", "presentazione delle domande", "termine ultimo"),
    "territory": ("territorio", "lombardia", "milano", "municipio"),
    "funding_min": ("contributo minimo", "importo minimo"),
    "funding_max": ("contributo massimo", "importo massimo"),
    "cofunding": ("cofinanziamento", "quota a carico"),
    "partnership_required": ("partenariato", "partnership", "capofila"),
    "RUNTS_requirement": ("runts", "registro unico nazionale"),
    "project_duration": ("durata del progetto", "durata progettuale"),
    "excluded_expenses": ("spese escluse", "non ammissibili", "beni durevoli"),
}
TOKEN_RE = re.compile(r"[\wÀ-ÿ]+", re.UNICODE)


def text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def canonical_url(url: str) -> str:
    parsed = urllib.parse.urlsplit(url)
    query = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
    query = [(key, value) for key, value in query if key.casefold() not in {"utm_source", "utm_medium", "utm_campaign", "fbclid"}]
    return urllib.parse.urlunsplit((parsed.scheme.casefold(), parsed.netloc.casefold(), parsed.path, urllib.parse.urlencode(query), ""))


@dataclass(frozen=True)
class RetrievalMetadata:
    bm25_score: float | None = None
    semantic_score: float | None = None
    rank: int = 0


@dataclass(frozen=True)
class EvidenceEnvelope:
    chunk_id: str
    source_id: str
    document_id: str
    canonical_url: str
    source_type: str
    page: int | None
    section: str | None
    anchor: str | None
    text: str
    text_hash: str
    retrieval: RetrievalMetadata = field(default_factory=RetrievalMetadata)

    def __post_init__(self) -> None:
        if not self.chunk_id or not self.source_id or not self.document_id:
            raise ValueError("evidence_identity_required")
        if self.source_type not in SOURCE_TYPES:
            raise ValueError("evidence_source_type_invalid")
        if self.text_hash != text_hash(self.text):
            raise ValueError("evidence_text_hash_invalid")
        if canonical_url(self.canonical_url) != self.canonical_url:
            raise ValueError("evidence_canonical_url_invalid")

    def tool_document(self) -> dict[str, str]:
        return {"document_id": self.chunk_id, "text": self.text}

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        return data


@dataclass(frozen=True)
class RetrievalResult:
    mode: str
    evidence: tuple[EvidenceEnvelope, ...]
    semantic_status: str
    fallback_used: bool
    fallback_reason: str | None
    invented_document_ids: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "evidence": [item.to_dict() for item in self.evidence],
            "semantic_status": self.semantic_status,
            "fallback_used": self.fallback_used,
            "fallback_reason": self.fallback_reason,
            "invented_document_ids": list(self.invented_document_ids),
        }


@dataclass(frozen=True)
class ExpectedDocument:
    document_type: str
    canonical_url: str | None = None
    critical: bool = False


@dataclass(frozen=True)
class AvailableDocument:
    document_id: str
    document_type: str
    canonical_url: str
    content_hash: str


@dataclass(frozen=True)
class DocumentCompleteness:
    status: str
    expected_documents: tuple[ExpectedDocument, ...]
    available_documents: tuple[AvailableDocument, ...]
    missing_documents: tuple[ExpectedDocument, ...]
    eligibility_status: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "expected_documents": [asdict(item) for item in self.expected_documents],
            "available_documents": [asdict(item) for item in self.available_documents],
            "missing_documents": [asdict(item) for item in self.missing_documents],
            "eligibility_status": self.eligibility_status,
        }


@dataclass(frozen=True)
class CitationValidation:
    valid: bool
    citation_ids: tuple[str, ...]
    invented_citation_ids: tuple[str, ...]
    provenance: tuple[dict[str, Any], ...]


def make_evidence(
    *,
    source_id: str,
    document_id: str,
    canonical_url_value: str,
    source_type: str,
    text: str,
    page: int | None = None,
    section: str | None = None,
    anchor: str | None = None,
    ordinal: int = 0,
) -> EvidenceEnvelope:
    normalized_url = canonical_url(canonical_url_value)
    digest = hashlib.sha256(
        f"{source_id}\n{document_id}\n{page}\n{section}\n{anchor}\n{ordinal}\n{text_hash(text)}".encode("utf-8")
    ).hexdigest()[:20]
    return EvidenceEnvelope(
        chunk_id=f"E{digest.upper()}",
        source_id=source_id,
        document_id=document_id,
        canonical_url=normalized_url,
        source_type=source_type,
        page=page,
        section=section,
        anchor=anchor,
        text=text,
        text_hash=text_hash(text),
    )


def _tokens(value: str) -> list[str]:
    return [item.casefold() for item in TOKEN_RE.findall(value) if len(item) > 1]


def bm25_rank(query: str, evidence: Iterable[EvidenceEnvelope]) -> list[EvidenceEnvelope]:
    rows = list(evidence)
    query_terms = _tokens(query)
    row_terms = [_tokens(row.text) for row in rows]
    document_frequency = Counter(term for terms in row_terms for term in set(terms))
    average_length = sum(map(len, row_terms)) / max(1, len(row_terms))
    scored: list[tuple[float, EvidenceEnvelope]] = []
    for row, terms in zip(rows, row_terms):
        counts = Counter(terms)
        score = 0.0
        for term in query_terms:
            frequency = counts[term]
            if not frequency:
                continue
            inverse = math.log(1 + (len(rows) - document_frequency[term] + 0.5) / (document_frequency[term] + 0.5))
            score += inverse * (frequency * 2.2) / (
                frequency + 1.2 * (0.25 + 0.75 * len(terms) / max(1, average_length))
            )
        scored.append((score, row))
    scored.sort(key=lambda item: (-item[0], item[1].chunk_id))
    return [replace(row, retrieval=replace(row.retrieval, bm25_score=score, rank=index)) for index, (score, row) in enumerate(scored, 1)]


SemanticInvoker = Callable[[str, list[dict[str, str]]], Any]


def semantic_rank(
    query: str,
    evidence: Iterable[EvidenceEnvelope],
    invoke: SemanticInvoker,
) -> tuple[list[EvidenceEnvelope], str, tuple[str, ...]]:
    rows = list(evidence)
    by_id = {row.chunk_id: row for row in rows}
    raw = invoke(query, [row.tool_document() for row in rows])
    if hasattr(raw, "model_dump"):
        raw = raw.model_dump(mode="json")
    if not isinstance(raw, dict):
        raise RuntimeError("semantic_result_non_object")
    if raw.get("ok") is False:
        raise RuntimeError(str(raw.get("error_type") or "semantic_tool_unavailable"))
    output = raw.get("output", raw)
    ranked = output.get("ranked") if isinstance(output, dict) else None
    if not isinstance(ranked, list):
        raise RuntimeError("semantic_ranked_missing")
    result: list[EvidenceEnvelope] = []
    invented: list[str] = []
    seen: set[str] = set()
    for item in ranked:
        if not isinstance(item, dict):
            continue
        chunk_id = str(item.get("document_id") or "")
        if chunk_id not in by_id:
            invented.append(chunk_id)
            continue
        if chunk_id in seen:
            continue
        seen.add(chunk_id)
        original = by_id[chunk_id]
        result.append(
            replace(
                original,
                retrieval=replace(
                    original.retrieval,
                    semantic_score=float(item.get("score") or 0.0),
                    rank=len(result) + 1,
                ),
            )
        )
    remaining = [row for row in rows if row.chunk_id not in seen]
    first_remaining_rank = len(result) + 1
    result.extend(
        replace(row, retrieval=replace(row.retrieval, rank=first_remaining_rank + index))
        for index, row in enumerate(remaining)
    )
    return result, "ready", tuple(sorted(set(invented)))


def hybrid_retrieve(
    query: str,
    evidence: Iterable[EvidenceEnvelope],
    *,
    semantic_invoke: SemanticInvoker | None,
    top_k: int = 5,
    candidate_k: int = 20,
    rrf_constant: int = 60,
) -> RetrievalResult:
    rows = list(evidence)
    lexical = bm25_rank(query, rows)
    if semantic_invoke is None:
        selected = tuple(replace(row, retrieval=replace(row.retrieval, rank=index)) for index, row in enumerate(lexical[:top_k], 1))
        return RetrievalResult("bm25_fallback_explicit", selected, "unavailable", True, "semantic_tool_unavailable")
    try:
        semantic, status, invented = semantic_rank(query, rows, semantic_invoke)
    except Exception as exc:
        selected = tuple(replace(row, retrieval=replace(row.retrieval, rank=index)) for index, row in enumerate(lexical[:top_k], 1))
        return RetrievalResult("bm25_fallback_explicit", selected, "unavailable", True, f"{type(exc).__name__}:{exc}")
    lexical_positions = {row.chunk_id: index for index, row in enumerate(lexical[:candidate_k], 1)}
    semantic_positions = {row.chunk_id: index for index, row in enumerate(semantic[:candidate_k], 1)}
    by_id = {row.chunk_id: row for row in rows}
    candidate_ids = set(lexical_positions) | set(semantic_positions)
    fused = []
    for chunk_id in candidate_ids:
        score = 0.0
        if chunk_id in lexical_positions:
            score += 1 / (rrf_constant + lexical_positions[chunk_id])
        if chunk_id in semantic_positions:
            score += 1 / (rrf_constant + semantic_positions[chunk_id])
        fused.append((score, chunk_id))
    fused.sort(key=lambda item: (-item[0], item[1]))
    selected = []
    for rank, (_, chunk_id) in enumerate(fused[:top_k], 1):
        original = by_id[chunk_id]
        lexical_row = next((row for row in lexical if row.chunk_id == chunk_id), original)
        semantic_row = next((row for row in semantic if row.chunk_id == chunk_id), original)
        selected.append(
            replace(
                original,
                retrieval=RetrievalMetadata(
                    bm25_score=lexical_row.retrieval.bm25_score,
                    semantic_score=semantic_row.retrieval.semantic_score,
                    rank=rank,
                ),
            )
        )
    return RetrievalResult("hybrid_rrf", tuple(selected), status, False, None, invented)


def retrieve_for_bandi(
    query: str,
    evidence: Iterable[EvidenceEnvelope],
    *,
    semantic_invoke: SemanticInvoker | None,
    top_k: int = 5,
    minimum_documents: int = 2,
    minimum_tokens: int = 500,
) -> RetrievalResult:
    rows = list(evidence)
    documents = {row.document_id for row in rows}
    token_count = sum(len(_tokens(row.text)) for row in rows)
    if len(documents) < minimum_documents and token_count < minimum_tokens:
        selected = tuple(bm25_rank(query, rows)[:top_k])
        return RetrievalResult("bm25_small_corpus", selected, "not_requested", False, None)
    return hybrid_retrieve(query, rows, semantic_invoke=semantic_invoke, top_k=top_k)


def evidence_context(evidence: Iterable[EvidenceEnvelope]) -> str:
    blocks = []
    for item in evidence:
        blocks.append(
            "\n".join(
                (
                    f"[EVIDENCE:{item.chunk_id}]",
                    f"source_id: {item.source_id}",
                    f"document_id: {item.document_id}",
                    f"page: {item.page if item.page is not None else 'null'}",
                    f"section: {item.section if item.section is not None else 'null'}",
                    f"anchor: {item.anchor if item.anchor is not None else 'null'}",
                    f"text_hash: {item.text_hash}",
                    f"text: {item.text}",
                )
            )
        )
    return "\n\n".join(blocks)


def validate_citations(citation_ids: Iterable[str], evidence: Iterable[EvidenceEnvelope]) -> CitationValidation:
    by_id = {row.chunk_id: row for row in evidence}
    ordered = tuple(dict.fromkeys(str(item) for item in citation_ids))
    invented = tuple(item for item in ordered if item not in by_id)
    provenance = tuple(
        {
            "citation_id": item,
            "source_id": by_id[item].source_id,
            "document_id": by_id[item].document_id,
            "canonical_url": by_id[item].canonical_url,
            "page": by_id[item].page,
            "section": by_id[item].section,
            "anchor": by_id[item].anchor,
            "text_hash": by_id[item].text_hash,
        }
        for item in ordered
        if item in by_id
    )
    return CitationValidation(not invented and bool(ordered), ordered, invented, provenance)


def claim_citations_from_evidence(
    evidence: Iterable[EvidenceEnvelope],
    *,
    limit_per_claim: int = 2,
) -> tuple[dict[str, list[str]], dict[str, tuple[dict[str, Any], ...]]]:
    rows = list(evidence)
    citations: dict[str, list[str]] = {}
    rendered: dict[str, tuple[dict[str, Any], ...]] = {}
    for field_name, terms in CLAIM_TERMS.items():
        matches = [
            row.chunk_id
            for row in rows
            if any(term in row.text.casefold() for term in terms)
        ][:limit_per_claim]
        if not matches:
            continue
        validation = validate_citations(matches, rows)
        if validation.valid:
            citations[field_name] = list(validation.citation_ids)
            rendered[field_name] = validation.provenance
    return citations, rendered


def assess_document_completeness(
    expected: Iterable[ExpectedDocument],
    available: Iterable[AvailableDocument],
) -> DocumentCompleteness:
    expected_rows = tuple(expected)
    available_rows = tuple(available)
    if not expected_rows:
        return DocumentCompleteness("unknown", expected_rows, available_rows, (), "conditional_missing_primary_documents")
    available_types = {item.document_type for item in available_rows}
    available_urls = {canonical_url(item.canonical_url) for item in available_rows}
    missing = tuple(
        item
        for item in expected_rows
        if (
            canonical_url(item.canonical_url) not in available_urls
            if item.canonical_url is not None
            else item.document_type not in available_types
        )
    )
    if not missing:
        status = "complete"
    elif any(item.critical or item.document_type in CRITICAL_DOCUMENT_TYPES for item in missing):
        status = "partial_critical"
    else:
        status = "partial_noncritical"
    eligibility = "conditional_missing_primary_documents" if status in {"partial_critical", "unknown"} else "evidence_available"
    return DocumentCompleteness(status, expected_rows, available_rows, missing, eligibility)


def guard_critical_claims(
    claims: dict[str, Any],
    claim_citations: dict[str, list[str]],
    evidence: Iterable[EvidenceEnvelope],
    completeness: DocumentCompleteness,
) -> tuple[dict[str, Any], dict[str, CitationValidation]]:
    guarded = dict(claims)
    validations: dict[str, CitationValidation] = {}
    for field_name in CRITICAL_CLAIMS:
        if field_name not in guarded:
            continue
        if guarded[field_name] is None or guarded[field_name] == "unknown" or guarded[field_name] == []:
            continue
        validation = validate_citations(claim_citations.get(field_name, []), evidence)
        validations[field_name] = validation
        if not validation.valid:
            guarded[field_name] = "unknown"
    if completeness.status in {"partial_critical", "unknown"}:
        if "eligibility" in guarded:
            guarded["eligibility"] = "unknown"
        guarded["eligibility_status"] = "conditional_missing_primary_documents"
    return guarded, validations


def detect_document_change(previous: AvailableDocument | None, current: AvailableDocument) -> str:
    if previous is None:
        return "new"
    if canonical_url(previous.canonical_url) != canonical_url(current.canonical_url):
        return "new_url"
    if previous.content_hash != current.content_hash:
        return "updated"
    return "unchanged"


def _document_type(label: str, href: str) -> tuple[str, bool]:
    value = f"{label} {href}".casefold()
    if any(token in value for token in ("proroga", "rettifica", "modifica", "decreto")):
        return "official_amendment", True
    if "faq" in value:
        return "official_faq", False
    if any(token in value for token in ("piano finanziario", "budget", "spese ammissibili")):
        return "financial_plan", True
    if any(token in value for token in ("requisiti", "beneficiari", "ammissibil")):
        return "eligibility_requirements", True
    if any(token in value for token in ("regolamento", "avviso", "testo del bando", "bando")):
        return "official_call_text", True
    if any(token in value for token in ("allegato", "modulo", "domanda", "formulario")):
        return "official_attachment", False
    return "related_document", False


def discover_official_documents(html: str, base_url: str) -> list[ExpectedDocument]:
    base = urllib.parse.urlsplit(base_url)
    soup = BeautifulSoup(html, "html.parser")
    found: dict[str, ExpectedDocument] = {}
    for node in soup.find_all("a", href=True):
        href = canonical_url(urllib.parse.urljoin(base_url, str(node.get("href"))))
        parsed = urllib.parse.urlsplit(href)
        if parsed.scheme not in {"http", "https"}:
            continue
        label = " ".join(node.get_text(" ", strip=True).split())
        document_type, critical = _document_type(label, href)
        if document_type == "related_document":
            continue
        base_tail = ".".join((base.hostname or "").split(".")[-3:])
        href_tail = ".".join((parsed.hostname or "").split(".")[-3:])
        if base_tail != href_tail:
            continue
        found[href] = ExpectedDocument(document_type, href, critical)
    return [found[key] for key in sorted(found)]
