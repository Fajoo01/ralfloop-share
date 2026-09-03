from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from enum import StrEnum
from typing import Protocol

from pydantic import AnyHttpUrl, Field

from ralfloop_agent.domains.source_discovery import SearchProvider

from .contracts import StrictModel
from .memory_service import MemoryDocument, MemoryService
from .platform import SourceRef


class ResearchSource(StrictModel):
    source_id: str = Field(pattern=r"^research\.[a-f0-9]{24}$")
    url: AnyHttpUrl
    title: str = Field(min_length=1, max_length=1000)
    observed_at: datetime
    published_at: datetime | None = None
    content_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    confidence: float = Field(ge=0, le=1)
    authoritative: bool = False
    excerpt: str | None = Field(default=None, max_length=6000)


class ResearchEvidence(StrictModel):
    source: ResearchSource
    passages: tuple[str, ...] = Field(max_length=20)


class ClaimVerdict(StrEnum):
    SUPPORTED = "SUPPORTED"
    CONTRADICTED = "CONTRADICTED"
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"
    CONFLICTING_SOURCES = "CONFLICTING_SOURCES"


class ClaimVerification(StrictModel):
    claim: str = Field(min_length=1, max_length=2000)
    verdict: ClaimVerdict
    evidence: tuple[ResearchEvidence, ...]
    confidence: float = Field(ge=0, le=1)


class OpenedWebSource(StrictModel):
    url: AnyHttpUrl
    title: str = Field(min_length=1, max_length=1000)
    text: str = Field(min_length=1, max_length=200_000)
    published_at: datetime | None = None


class ResearchOpenBackend(Protocol):
    def open(self, url: str) -> OpenedWebSource: ...


class SafeWebOpenBackend:
    """Adapter over existing SSRF-safe bounded public-web reader."""

    def open(self, url: str) -> OpenedWebSource:
        from ralfloop_agent.model_tools.web_research import web_open
        result = web_open(url)
        return OpenedWebSource(
            url=result["url"], title=result["title"] or result["url"],
            text=result["text"],
        )


class WebResearchService:
    def __init__(
        self, search: SearchProvider, opener: ResearchOpenBackend,
        *, memory: MemoryService | None = None, max_sources: int = 100,
    ) -> None:
        self.search_backend = search
        self.opener = opener
        self.memory = memory
        self.max_sources = max_sources
        self._sources: dict[str, ResearchSource] = {}

    def search_web(self, query: str, *, limit: int = 10) -> tuple[ResearchSource, ...]:
        if not 1 <= limit <= 30 or not query.strip():
            raise ValueError("research_query_invalid")
        output = []
        for candidate in self.search_backend.search(query, limit=limit):
            identity = _source_id(candidate.url)
            source = ResearchSource(
                source_id=identity, url=candidate.url, title=candidate.title or candidate.url,
                observed_at=datetime.now(timezone.utc), confidence=max(0.1, 1.0 - candidate.rank * 0.04),
                authoritative=_authoritative(candidate.url),
                excerpt=(candidate.snippet or None),
            )
            self._remember(source)
            output.append(source)
        return tuple(output)

    def find_authoritative_source(self, query: str, *, limit: int = 10) -> tuple[ResearchSource, ...]:
        return tuple(row for row in self.search_web(query, limit=limit) if row.authoritative)

    def open_source(self, source_id: str) -> ResearchSource:
        source = self._get(source_id)
        opened = self.opener.open(str(source.url))
        if str(opened.url).rstrip("/") != str(source.url).rstrip("/"):
            raise ValueError("research_source_identity_changed")
        digest = hashlib.sha256(opened.text.encode()).hexdigest()
        updated = source.model_copy(update={
            "title": opened.title, "published_at": opened.published_at,
            "content_hash": digest, "excerpt": opened.text[:6000],
        })
        self._sources[source_id] = updated
        if self.memory is not None:
            ref = SourceRef(
                system="web_research", native_id=source_id, locator=str(source.url),
                observed_at=updated.observed_at.isoformat(), content_hash=digest,
            )
            self.memory.put_document(MemoryDocument.build(
                document_id="document." + source_id, title=opened.title,
                body=opened.text, source=ref,
            ))
        return updated

    def extract_evidence(self, source_id: str, terms: tuple[str, ...]) -> ResearchEvidence:
        source = self._get(source_id)
        if source.content_hash is None or not source.excerpt:
            source = self.open_source(source_id)
        tokens = tuple(term.casefold().strip() for term in terms if term.strip())
        passages = tuple(
            sentence.strip() for sentence in re.split(r"(?<=[.!?])\s+|\n+", source.excerpt or "")
            if sentence.strip() and any(term in sentence.casefold() for term in tokens)
        )[:20]
        return ResearchEvidence(source=source, passages=passages)

    def compare_sources(self, source_ids: tuple[str, ...], terms: tuple[str, ...]) -> tuple[ResearchEvidence, ...]:
        if not 2 <= len(source_ids) <= 10 or len(set(source_ids)) != len(source_ids):
            raise ValueError("research_comparison_sources_invalid")
        return tuple(self.extract_evidence(identity, terms) for identity in source_ids)

    def verify_claim(self, claim: str, source_ids: tuple[str, ...]) -> ClaimVerification:
        terms = tuple(token for token in re.findall(r"[\wÀ-ÿ-]+", claim.casefold()) if len(token) > 3)[:16]
        evidence = tuple(self.extract_evidence(identity, terms) for identity in source_ids)
        supported = sum(bool(row.passages) for row in evidence)
        polarities = {
            any(re.search(r"\b(non|not|vietat[oa]|esclus[oa])\b", passage.casefold()) for passage in row.passages)
            for row in evidence if row.passages
        }
        if len(polarities) > 1:
            verdict = ClaimVerdict.CONFLICTING_SOURCES
        elif polarities == {True}:
            verdict = ClaimVerdict.CONTRADICTED
        else:
            verdict = ClaimVerdict.SUPPORTED if supported and supported == len(evidence) else ClaimVerdict.INSUFFICIENT_EVIDENCE
        return ClaimVerification(
            claim=claim, verdict=verdict, evidence=evidence,
            confidence=supported / len(evidence) if evidence else 0,
        )

    def _get(self, source_id: str) -> ResearchSource:
        try:
            return self._sources[source_id]
        except KeyError as exc:
            raise ValueError("research_source_not_discovered") from exc

    def _remember(self, source: ResearchSource) -> None:
        if source.source_id not in self._sources and len(self._sources) >= self.max_sources:
            raise ValueError("research_source_quota_exceeded")
        self._sources[source.source_id] = source


def _source_id(url: str) -> str:
    return "research." + hashlib.sha256(url.encode()).hexdigest()[:24]


def _authoritative(url: str) -> bool:
    from urllib.parse import urlparse
    host = (urlparse(url).hostname or "").casefold()
    return host.endswith((".gov.it", ".europa.eu")) or host in {
        "comune.milano.it", "www.comune.milano.it", "servizi.comune.milano.it",
        "regione.lombardia.it", "bandi.regione.lombardia.it",
    }


__all__ = ["ClaimVerification", "ClaimVerdict", "OpenedWebSource", "ResearchEvidence", "ResearchOpenBackend", "ResearchSource", "SafeWebOpenBackend", "WebResearchService"]
