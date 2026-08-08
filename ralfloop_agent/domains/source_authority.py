from __future__ import annotations

import hashlib
import re
import urllib.parse
from dataclasses import asdict, dataclass, field


@dataclass
class SourceCandidate:
    candidate_id: str
    url: str
    title: str = ""
    publisher: str = ""
    issuer: str = ""
    document_type: str = "unknown"
    content_type: str = ""
    checksum: str = ""
    publication_date: str | None = None
    version: str | None = None
    edition: str | None = None
    snippet: str = ""
    direct_document: bool = False
    metadata: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class SourceAuthorityAssessment:
    candidate_id: str
    publisher: str
    publisher_domain: str
    issuer_match: bool
    official_domain: bool
    document_type: str
    direct_document: bool
    publication_date: str | None
    version: str | None
    edition: str | None
    https: bool
    stable_url: bool
    content_type: str
    checksum: str
    signature_or_protocol: bool
    archived: bool
    primary_or_secondary: str
    binding_level: str
    deterministic_gate: bool
    authority_level: str
    binding_eligible: bool
    reasons: list[str] = field(default_factory=list)
    concerns: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


def assess_authority(candidate: SourceCandidate, *, issuer: str = "", official_domains: list[str] | None = None) -> SourceAuthorityAssessment:
    parsed = urllib.parse.urlparse(candidate.url)
    domain = (parsed.hostname or "").lower()
    https = parsed.scheme == "https"
    official_domains = official_domains or infer_official_domains(issuer)
    official_domain = any(domain == item or domain.endswith("." + item) for item in official_domains)
    expected_title = str(candidate.metadata.get("expected_title") or "")
    expected_terms = _query_terms(expected_title, candidate.metadata.get("bando_id", ""))
    text = _norm(" ".join([candidate.publisher, candidate.title, candidate.snippet, domain, candidate.url]))
    issuer_match = official_domain or _issuer_matches(issuer, text)
    relevant = official_domain or _relevant_to_bando(text, expected_terms)
    direct_document = candidate.direct_document or candidate.url.lower().split("?")[0].endswith((".pdf", ".docx", ".txt", ".json"))
    unstable_domain = bool(re.search(r"\b(facebook|instagram|x\.com|twitter|linkedin|forum|blogspot|medium|substack)\b", domain))
    stable_url = https and bool(domain) and not unstable_domain
    forbidden = _looks_generated_or_snippet(candidate)
    unverified = unstable_domain or not relevant or _looks_seo_or_mirror(candidate)
    hard_gate = all(
        [
            bool(candidate.publisher or domain),
            bool(candidate.url),
            bool(candidate.checksum),
            https,
            stable_url,
            bool(candidate.title or candidate.document_type != "unknown"),
            not forbidden,
            not unverified,
            candidate.document_type != "search_snippet",
        ]
    )
    if official_domain and issuer_match and candidate.document_type in {"primary_official_document", "official_amendment", "official_deadline_extension"}:
        level = "A"
        binding = "binding_official"
        primary = "primary"
    elif official_domain and issuer_match and candidate.document_type in {
        "official_faq",
        "official_attachment",
        "official_application_form",
        "official_budget_template",
        "official_operational_manual",
        "official_reporting_manual",
        "official_archive_page",
    }:
        level = "B"
        binding = "binding_official"
        primary = "secondary"
    elif forbidden or unverified or candidate.document_type == "search_snippet":
        level = "D"
        binding = "reject"
        primary = "unverified"
    elif not official_domain and relevant and (candidate.publisher or domain):
        level = "C"
        binding = "supporting"
        primary = "supporting"
    else:
        level = "D"
        binding = "reject"
        primary = "unverified"
    binding_eligible = hard_gate and level in {"A", "B"}
    reasons = []
    concerns = []
    if official_domain:
        reasons.append("official_domain_match")
    if issuer_match:
        reasons.append("issuer_match")
    if direct_document:
        reasons.append("direct_document")
    if not hard_gate:
        concerns.append("hard_gate_failed")
    if forbidden:
        concerns.append("generated_or_snippet")
    if unverified:
        concerns.append("unverified_or_not_relevant")
    if unstable_domain:
        concerns.append("unstable_social_or_blog_domain")
    if not relevant:
        concerns.append("not_relevant_to_bando")
    return SourceAuthorityAssessment(
        candidate.candidate_id,
        candidate.publisher,
        domain,
        issuer_match,
        official_domain,
        candidate.document_type,
        direct_document,
        candidate.publication_date,
        candidate.version,
        candidate.edition,
        https,
        stable_url,
        candidate.content_type,
        candidate.checksum,
        bool(candidate.metadata.get("signature_or_protocol")),
        bool(candidate.metadata.get("archived")),
        primary,
        binding,
        hard_gate,
        level,
        binding_eligible,
        reasons,
        concerns,
    )


def infer_official_domains(issuer: str) -> list[str]:
    low = _norm(issuer)
    if "fondazione cariplo" in low:
        return ["fondazionecariplo.it"]
    if "fondazione unipolis" in low:
        return ["fondazioneunipolis.org", "fondazioneunipolis.it"]
    words = [w for w in re.split(r"[^a-z0-9]+", low) if w and w != "fondazione"]
    return ["".join(words) + ".it"] if words else []


def candidate_from_search(result, *, issuer: str = "", content: bytes | None = None, content_type: str = "") -> SourceCandidate:
    checksum = hashlib.sha256(content or b"").hexdigest() if content is not None else ""
    return SourceCandidate(
        candidate_id=result.candidate_id,
        url=result.url,
        title=result.title,
        publisher=result.publisher,
        issuer=issuer,
        document_type=classify_document_type(result.title, result.url, result.snippet),
        content_type=content_type,
        checksum=checksum,
        snippet=result.snippet,
        direct_document=result.url.lower().split("?")[0].endswith((".pdf", ".docx", ".txt", ".json")),
        metadata=dict(result.metadata or {}),
    )


def classify_document_type(title: str, url: str = "", snippet: str = "") -> str:
    text = _norm(" ".join([title, url, snippet]))
    if "snippet" in text:
        return "search_snippet"
    if "faq" in text or "domande frequenti" in text:
        return "official_faq"
    if "rettifica" in text:
        return "official_amendment"
    if "proroga" in text:
        return "official_deadline_extension"
    if "rendicont" in text:
        return "official_reporting_manual"
    if "manuale" in text or "guida" in text:
        return "official_operational_manual"
    if "budget" in text:
        return "official_budget_template"
    if "formulario" in text or "modulistica" in text:
        return "official_application_form"
    if "allegat" in text:
        return "official_attachment"
    if "regolamento" in text or "bando" in text:
        return "primary_official_document"
    return "unknown"


def _looks_generated_or_snippet(candidate: SourceCandidate) -> bool:
    text = _norm(" ".join([candidate.title, candidate.snippet, candidate.publisher]))
    return any(token in text for token in ("llm generated", "search snippet", "unverified summary", "riassunto"))


def _looks_seo_or_mirror(candidate: SourceCandidate) -> bool:
    parsed = urllib.parse.urlparse(candidate.url)
    domain = (parsed.hostname or "").lower()
    text = _norm(" ".join([domain, candidate.title, candidate.snippet]))
    return any(token in text for token in ("seo", "mirror", "aggregator", "aggregatore", "casino", "coupon", "telegram", "facebook", "forum"))


def _issuer_matches(issuer: str, text: str) -> bool:
    words = [word for word in re.split(r"[^a-z0-9]+", _norm(issuer)) if len(word) > 2]
    if not words:
        return False
    return all(word in text for word in words[:2]) or "".join(words[:2]) in text.replace(" ", "")


def _query_terms(title: str, bando_id: str = "") -> set[str]:
    raw = _norm(" ".join([title, bando_id.replace("_", " ")]))
    words = {word for word in re.split(r"[^a-z0-9]+", raw) if len(word) > 2}
    return {word for word in words if word not in {"bando", "bandi", "fondazione", "2026", "2025", "2024"}}


def _relevant_to_bando(text: str, expected_terms: set[str]) -> bool:
    if not expected_terms:
        return True
    if "act" in expected_terms and "act" in text:
        return True
    if "nuovi" in expected_terms and "ponti" in expected_terms and "nuovi" in text and "ponti" in text:
        return True
    hits = sum(1 for term in expected_terms if term in text)
    return hits >= min(2, len(expected_terms))


def _norm(value: str) -> str:
    return re.sub(r"\s+", " ", value.lower()).strip()
