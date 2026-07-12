from __future__ import annotations

import json
import os
import time
import urllib.parse
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .source_authority import SourceAuthorityAssessment, SourceCandidate, assess_authority, candidate_from_search, infer_official_domains
from .source_discovery import ConfiguredSearchProvider, SearchProvider, SearchProviderUnavailable, SearchQuery, SearchResultCandidate, generate_bando_queries
from .source_fetcher import FetchedSource, SourceFetcher
from .source_jury import SourceConflictAssessment, SourceJury, SourceJuryAssessment
from .storage import append_jsonl


@dataclass
class WebResearchRequest:
    bando_id: str = ""
    version: str = ""
    title: str = ""
    issuer: str = ""
    edition: str = ""
    territory: str = ""
    official_domain: str = ""
    missing_documents: list[str] = field(default_factory=list)
    queries: list[str] = field(default_factory=list)
    allow_web: bool = False
    offline: bool = False
    max_queries: int = 12
    max_results_per_query: int = 10
    max_unique_candidates: int = 50
    max_full_fetches: int = 20
    max_official_fetches: int = 15
    max_supporting_fetches: int = 5
    max_discovery_depth: int = 2
    max_jury_candidates: int = 8
    max_download_bytes: int = 52_428_800
    timeout_sec: float = 30.0
    cache_dir: str = ".ralf_run/bando_web_cache"
    user_agent: str = "Ralfloop-BandoResearch/1.0"

    @classmethod
    def from_env(cls, **values: Any) -> "WebResearchRequest":
        return cls(
            max_queries=int(os.getenv("RALFLOOP_BANDO_WEB_MAX_QUERIES", "12")),
            max_results_per_query=int(os.getenv("RALFLOOP_BANDO_WEB_MAX_RESULTS_PER_QUERY", "8")),
            max_unique_candidates=int(os.getenv("RALFLOOP_BANDO_WEB_MAX_UNIQUE_CANDIDATES", "50")),
            max_full_fetches=int(os.getenv("RALFLOOP_BANDO_WEB_MAX_FULL_FETCHES", "20")),
            max_official_fetches=int(os.getenv("RALFLOOP_BANDO_WEB_MAX_OFFICIAL_FETCHES", "15")),
            max_supporting_fetches=int(os.getenv("RALFLOOP_BANDO_WEB_MAX_SUPPORTING_FETCHES", "5")),
            max_discovery_depth=int(os.getenv("RALFLOOP_BANDO_WEB_MAX_DISCOVERY_DEPTH", "2")),
            max_jury_candidates=int(os.getenv("RALFLOOP_BANDO_WEB_MAX_JURY_CANDIDATES", "8")),
            max_download_bytes=int(os.getenv("RALFLOOP_BANDO_WEB_MAX_DOWNLOAD_BYTES", "52428800")),
            timeout_sec=float(os.getenv("RALFLOOP_BANDO_WEB_TIMEOUT_SEC", "30")),
            cache_dir=os.getenv("RALFLOOP_BANDO_WEB_CACHE_DIR", ".ralf_run/bando_web_cache"),
            user_agent=os.getenv("RALFLOOP_BANDO_WEB_USER_AGENT", "Ralfloop-BandoResearch/1.0"),
            **values,
        )

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class WebResearchResult:
    ok: bool
    status: str
    research_id: str
    bando_id: str = ""
    version: str = ""
    queries: list[dict] = field(default_factory=list)
    provider: str = ""
    candidates: list[dict] = field(default_factory=list)
    authority_assessments: list[dict] = field(default_factory=list)
    jury_assessments: list[dict] = field(default_factory=list)
    fetched_sources: list[dict] = field(default_factory=list)
    accepted_sources: list[dict] = field(default_factory=list)
    rejected_sources: list[dict] = field(default_factory=list)
    conflicts: list[dict] = field(default_factory=list)
    new_documents: list[dict] = field(default_factory=list)
    updated_documents: list[dict] = field(default_factory=list)
    validation_required: bool = False
    human_review_required: bool = False
    errors: list[str] = field(default_factory=list)
    jury_invoked: bool = False
    downloads: list[dict] = field(default_factory=list)
    budget_exhausted: bool = False
    binding_accepted_count: int = 0
    supporting_accepted_count: int = 0
    discovery_only_count: int = 0
    rejected_count: int = 0
    not_fetched_count: int = 0
    duplicate_count: int = 0
    fetch_failed_count: int = 0
    jury_reviewed_count: int = 0
    human_review_count: int = 0
    duplicate_urls: list[dict] = field(default_factory=list)
    duplicate_content: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


class BandoWebResearcher:
    def __init__(
        self,
        *,
        provider: SearchProvider | None = None,
        fetcher: SourceFetcher | None = None,
        jury: SourceJury | None = None,
        audit_path: str | Path = "logs/bando_web_research.jsonl",
    ) -> None:
        self.provider = provider
        self.fetcher = fetcher
        self.jury = jury or SourceJury()
        self.audit_path = Path(audit_path)

    def research(self, request: WebResearchRequest, *, provider: SearchProvider | None = None, fetcher: SourceFetcher | None = None) -> WebResearchResult:
        research_id = str(uuid.uuid4())
        if request.offline or not _web_enabled(request):
            result = WebResearchResult(False, "web_disabled", research_id, request.bando_id, request.version)
            self._audit(request, result)
            return result
        provider = provider or self.provider or ConfiguredSearchProvider(timeout_sec=request.timeout_sec)
        fetcher = fetcher or self.fetcher or SourceFetcher(cache_dir=request.cache_dir, timeout_sec=request.timeout_sec, max_bytes=request.max_download_bytes, user_agent=request.user_agent)
        queries = self._queries(request)
        candidates: list[SourceCandidate] = []
        errors: list[str] = []
        for query in queries:
            try:
                for item in provider.search(query.query, limit=request.max_results_per_query):
                    candidates.append(_source_candidate(item, request))
            except SearchProviderUnavailable as exc:
                status = _provider_status(str(exc))
                if status == "provider_empty_result":
                    errors.append(f"{status}:{query.query}")
                    continue
                result = WebResearchResult(False, status, research_id, request.bando_id, request.version, [q.to_dict() for q in queries], type(provider).__name__, errors=[status])
                self._audit(request, result)
                return result
        candidates = _expand_discovered_official_links(candidates, request)
        candidates, duplicate_urls = _dedupe_by_url(candidates)
        candidates = _rank_candidates(candidates, request)[: request.max_unique_candidates]
        if not candidates:
            status = "provider_empty_result" if errors else "no_candidates"
            result = WebResearchResult(True, status, research_id, request.bando_id, request.version, [q.to_dict() for q in queries], type(provider).__name__, errors=errors)
            self._audit(request, result)
            return result
        authority_rows: list[SourceAuthorityAssessment] = []
        jury_rows: list[SourceJuryAssessment] = []
        accepted: list[dict] = []
        rejected: list[dict] = []
        fetched: list[FetchedSource] = []
        not_fetched: list[dict] = []
        duplicate_content: list[dict] = []
        seen_checksums: dict[str, str] = {}
        official_domains = infer_official_domains(request.issuer)
        selected, skipped = _select_fetch_candidates(candidates, request, official_domains)
        not_fetched.extend(item.to_dict() for item in skipped)
        for candidate in selected:
            preliminary = assess_authority(candidate, issuer=request.issuer, official_domains=[request.official_domain] if request.official_domain else official_domains)
            fetched_source = self._fetch_for_full_gate(candidate, preliminary, fetcher)
            if fetched_source is not None:
                fetched.append(fetched_source)
                if fetched_source.ok:
                    _enrich_candidate_from_fetch(candidate, fetched_source)
                    if candidate.checksum in seen_checksums:
                        duplicate_content.append(
                            {
                                "duplicate_url": candidate.url,
                                "checksum": candidate.checksum,
                                "canonical_source_id": seen_checksums[candidate.checksum],
                                "duplicate_provenance": candidate.metadata.get("search_provenance", []),
                            }
                        )
                        continue
                    seen_checksums[candidate.checksum] = candidate.candidate_id
            authority = assess_authority(candidate, issuer=request.issuer, official_domains=[request.official_domain] if request.official_domain else official_domains)
            jury = self.jury.assess(candidate, authority)
            authority_rows.append(authority)
            jury_rows.append(jury)
            if _accepted(authority, jury):
                row = candidate.to_dict() | {"authority": authority.to_dict(), "jury": jury.to_dict(), "fetch": fetched_source.to_dict()}
                accepted.append(row)
            else:
                row = candidate.to_dict() | {"authority": authority.to_dict(), "jury": jury.to_dict()}
                if fetched_source is not None:
                    row["fetch"] = fetched_source.to_dict()
                rejected.append(row)
        for candidate in skipped:
            authority = assess_authority(candidate, issuer=request.issuer, official_domains=[request.official_domain] if request.official_domain else official_domains)
            if authority.authority_level == "D":
                jury = self.jury.assess(candidate, authority)
                authority_rows.append(authority)
                jury_rows.append(jury)
                rejected.append(candidate.to_dict() | {"authority": authority.to_dict(), "jury": jury.to_dict(), "not_fetched": True})
        status = _status(accepted, rejected, jury_rows)
        binding = [a for a in accepted if a.get("jury", {}).get("recommended_use") == "binding"]
        supporting = [a for a in accepted if a.get("jury", {}).get("recommended_use") == "supporting"]
        discovery_only = [r for r in rejected if r.get("jury", {}).get("recommended_use") == "discovery_only"]
        fetch_failed = [f for f in fetched if not f.ok]
        budget_exhausted = bool(
            len(candidates) > len(selected)
            or len(fetched) >= request.max_full_fetches
            or len(duplicate_urls)
            or len(duplicate_content)
        )
        result = WebResearchResult(
            bool(accepted or rejected),
            status,
            research_id,
            request.bando_id,
            request.version,
            [q.to_dict() for q in queries],
            type(provider).__name__,
            [c.to_dict() for c in candidates],
            [a.to_dict() for a in authority_rows],
            [j.to_dict() for j in jury_rows],
            [f.to_dict() for f in fetched],
            accepted,
            rejected,
            [],
            binding,
            [],
            validation_required=bool(accepted),
            human_review_required=bool(accepted) or any(j.human_review_required for j in jury_rows if j.recommended_use != "reject"),
            errors=errors,
            jury_invoked=True,
            downloads=[f.to_dict() for f in fetched],
            budget_exhausted=budget_exhausted,
            binding_accepted_count=len(binding),
            supporting_accepted_count=len(supporting),
            discovery_only_count=len(discovery_only),
            rejected_count=len(rejected),
            not_fetched_count=len(not_fetched),
            duplicate_count=len(duplicate_urls) + len(duplicate_content),
            fetch_failed_count=len(fetch_failed),
            jury_reviewed_count=len(jury_rows),
            human_review_count=sum(1 for j in jury_rows if j.human_review_required),
            duplicate_urls=duplicate_urls,
            duplicate_content=duplicate_content,
        )
        self._audit(request, result)
        return result

    def assess_conflict(self, local: dict, web: dict) -> SourceConflictAssessment:
        return self.jury.assess_conflict(local, web)

    def _queries(self, request: WebResearchRequest) -> list[SearchQuery]:
        if request.queries:
            queries = [SearchQuery(q, "manual") for q in request.queries]
        else:
            queries = generate_bando_queries(title=request.title, issuer=request.issuer, edition=request.edition, territory=request.territory, official_domain=request.official_domain)
        missing = [item.replace("_", " ") for item in request.missing_documents]
        for item in missing:
            if request.title:
                queries.append(SearchQuery(f'"{request.title}" {item}', item))
        return queries[: request.max_queries]

    def _fetch_if_needed(self, candidate: SourceCandidate, fetcher: SourceFetcher) -> FetchedSource:
        if candidate.metadata.get("skip_fetch"):
            return FetchedSource(True, "fetched", candidate.url, final_url=candidate.url, content_type=candidate.content_type, checksum=candidate.checksum, size_bytes=int(candidate.metadata.get("size_bytes") or 0))
        return fetcher.fetch(candidate.url)

    def _fetch_for_full_gate(self, candidate: SourceCandidate, preliminary: SourceAuthorityAssessment, fetcher: SourceFetcher) -> FetchedSource | None:
        if candidate.checksum and candidate.content_type:
            return self._fetch_if_needed(candidate, fetcher)
        if _eligible_for_safe_fetch(candidate, preliminary):
            return self._fetch_if_needed(candidate, fetcher)
        return None

    def _audit(self, request: WebResearchRequest, result: WebResearchResult) -> None:
        try:
            append_jsonl(
                self.audit_path,
                {
                    "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    "research_id": result.research_id,
                    "bando_id": request.bando_id,
                    "version": request.version,
                    "queries": [q.get("query") for q in result.queries],
                    "provider": result.provider,
                    "candidate_count": len(result.candidates),
                    "accepted_count": len(result.accepted_sources),
                    "binding_accepted_count": result.binding_accepted_count,
                    "supporting_accepted_count": result.supporting_accepted_count,
                    "discovery_only_count": result.discovery_only_count,
                    "rejected_count": len(result.rejected_sources),
                    "not_fetched_count": result.not_fetched_count,
                    "duplicate_count": result.duplicate_count,
                    "fetch_failed_count": result.fetch_failed_count,
                    "jury_reviewed_count": result.jury_reviewed_count,
                    "official_count": sum(1 for item in result.jury_assessments if item.get("official")),
                    "supporting_count": sum(1 for item in result.jury_assessments if item.get("recommended_use") == "supporting"),
                    "jury_invoked": result.jury_invoked,
                    "jury_backend": "source_jury",
                    "authority_assessments": result.authority_assessments,
                    "downloads": result.downloads,
                    "checksums": [item.get("checksum") for item in result.downloads if item.get("checksum")],
                    "conflicts": result.conflicts,
                    "new_documents": result.new_documents,
                    "updated_documents": result.updated_documents,
                    "validation_required": result.validation_required,
                    "human_review_required": result.human_review_required,
                    "errors": result.errors,
                },
            )
        except Exception:
            pass


def _web_enabled(request: WebResearchRequest) -> bool:
    return request.allow_web or os.getenv("RALFLOOP_ENABLE_BANDO_WEB_RESEARCH") == "1"


def _provider_status(value: str) -> str:
    known = {
        "search_provider_unavailable",
        "unsupported_search_provider",
        "provider_unavailable",
        "provider_timeout",
        "provider_http_error",
        "provider_rate_limited",
        "provider_invalid_json",
        "provider_json_format_disabled",
        "provider_empty_result",
        "provider_partial_result",
        "provider_endpoint_not_allowlisted",
    }
    return value if value in known else "search_provider_unavailable"


def _source_candidate(item: SearchResultCandidate, request: WebResearchRequest) -> SourceCandidate:
    candidate = candidate_from_search(item, issuer=request.issuer)
    metadata = dict(item.metadata or {})
    metadata.setdefault("expected_title", request.title)
    metadata.setdefault("bando_id", request.bando_id)
    metadata.setdefault("issuer", request.issuer)
    metadata.setdefault("official_domain", request.official_domain)
    metadata.setdefault("search_provenance", [{"query": item.query, "url": item.url, "rank": item.rank}])
    for field in ("checksum", "content_type", "document_type", "publisher", "publication_date", "version", "edition"):
        value = metadata.get(field)
        if value:
            setattr(candidate, field, value)
    if metadata.get("published_at") and not candidate.publication_date:
        candidate.publication_date = metadata["published_at"]
    if metadata.get("direct_document") is not None:
        candidate.direct_document = bool(metadata.get("direct_document"))
    return candidate


def _dedupe_by_url(candidates: list[SourceCandidate]) -> tuple[list[SourceCandidate], list[dict]]:
    seen: dict[str, SourceCandidate] = {}
    duplicates: list[dict] = []
    for candidate in candidates:
        canonical = _canonical(candidate.url)
        if canonical in seen:
            original = seen[canonical]
            original.metadata.setdefault("search_provenance", []).extend(candidate.metadata.get("search_provenance", []))
            duplicates.append(
                {
                    "duplicate_url": candidate.url,
                    "canonical_url": canonical,
                    "canonical_source_id": original.candidate_id,
                    "duplicate_provenance": candidate.metadata.get("search_provenance", []),
                }
            )
            continue
        candidate.url = canonical
        seen[canonical] = candidate
    return list(seen.values()), duplicates


def _rank_candidates(candidates: list[SourceCandidate], request: WebResearchRequest) -> list[SourceCandidate]:
    official_domains = [request.official_domain] if request.official_domain else infer_official_domains(request.issuer)
    scored = []
    for candidate in candidates:
        authority = assess_authority(candidate, issuer=request.issuer, official_domains=official_domains)
        score = _rank_score(candidate, authority, request)
        candidate.metadata["preliminary_rank_score"] = score
        candidate.metadata["preliminary_authority_level"] = authority.authority_level
        scored.append((score, candidate))
    scored.sort(key=lambda item: item[0], reverse=True)
    return [candidate for _, candidate in scored]


def _rank_score(candidate: SourceCandidate, authority: SourceAuthorityAssessment, request: WebResearchRequest) -> int:
    score = 0
    if authority.official_domain:
        score += 100
    if authority.direct_document:
        score += 30
    if authority.issuer_match:
        score += 25
    if authority.document_type in {"primary_official_document", "official_amendment", "official_deadline_extension"}:
        score += 20
    elif authority.document_type in {"official_faq", "official_application_form", "official_budget_template", "official_reporting_manual", "official_archive_page"}:
        score += 12
    text = " ".join([candidate.title, candidate.url, candidate.snippet]).lower()
    if request.edition and request.edition.lower() in text:
        score += 8
    if any(part in text for part in ("/band", "/document", "/allegat", "/wp-content/uploads")):
        score += 6
    if authority.authority_level == "C":
        score -= 20
    if authority.authority_level == "D":
        score -= 100
    if "hard_gate_failed" in authority.concerns and authority.authority_level not in {"A", "B"}:
        score -= 10
    return score


def _select_fetch_candidates(
    candidates: list[SourceCandidate],
    request: WebResearchRequest,
    official_domains: list[str],
) -> tuple[list[SourceCandidate], list[SourceCandidate]]:
    selected: list[SourceCandidate] = []
    skipped: list[SourceCandidate] = []
    official_fetches = 0
    supporting_fetches = 0
    for candidate in candidates:
        authority = assess_authority(candidate, issuer=request.issuer, official_domains=[request.official_domain] if request.official_domain else official_domains)
        if len(selected) >= request.max_full_fetches:
            skipped.append(candidate)
            continue
        if authority.authority_level in {"A", "B"}:
            if official_fetches >= request.max_official_fetches:
                skipped.append(candidate)
                continue
            selected.append(candidate)
            official_fetches += 1
            continue
        if authority.authority_level == "C":
            if supporting_fetches >= request.max_supporting_fetches:
                skipped.append(candidate)
                continue
            selected.append(candidate)
            supporting_fetches += 1
            continue
        skipped.append(candidate)
    return selected, skipped


def _expand_discovered_official_links(candidates: list[SourceCandidate], request: WebResearchRequest) -> list[SourceCandidate]:
    out = list(candidates)
    seen = {item.url for item in out}
    for candidate in candidates:
        url = candidate.metadata.get("discovered_official_url")
        if not url or url in seen:
            continue
        seen.add(url)
        out.append(
            SourceCandidate(
                candidate_id=f"{candidate.candidate_id}:official",
                url=url,
                title=str(candidate.metadata.get("discovered_official_title") or candidate.title),
                publisher=request.issuer,
                issuer=request.issuer,
                document_type=str(candidate.metadata.get("discovered_document_type") or "primary_official_document"),
                content_type=str(candidate.metadata.get("discovered_content_type") or "application/pdf"),
                checksum=str(candidate.metadata.get("discovered_checksum") or ""),
                publication_date=candidate.metadata.get("discovered_publication_date"),
                version=candidate.metadata.get("discovered_version"),
                direct_document=True,
                metadata={
                    "discovered_from": candidate.candidate_id,
                    "skip_fetch": candidate.metadata.get("skip_fetch_discovered", False),
                    "expected_title": request.title,
                    "bando_id": request.bando_id,
                    "issuer": request.issuer,
                    "official_domain": request.official_domain,
                },
            )
        )
    return out


def _accepted(authority: SourceAuthorityAssessment, jury: SourceJuryAssessment) -> bool:
    if jury.recommended_use == "binding":
        return authority.deterministic_gate and authority.authority_level in {"A", "B"} and authority.binding_eligible
    if jury.recommended_use == "supporting":
        return authority.authority_level == "C"
    return False


def _eligible_for_safe_fetch(candidate: SourceCandidate, preliminary: SourceAuthorityAssessment) -> bool:
    if preliminary.document_type == "search_snippet" or "generated_or_snippet" in preliminary.concerns:
        return False
    if not candidate.url.startswith(("http://", "https://")):
        return False
    if preliminary.authority_level in {"A", "B"}:
        return True
    if preliminary.authority_level == "C" and (candidate.direct_document or candidate.metadata.get("provider") == "searxng"):
        return True
    return False


def _enrich_candidate_from_fetch(candidate: SourceCandidate, fetched: FetchedSource) -> None:
    candidate.url = fetched.final_url or candidate.url
    candidate.checksum = fetched.checksum
    candidate.content_type = fetched.content_type
    candidate.metadata["fetched_size_bytes"] = fetched.size_bytes
    candidate.metadata["cache_path"] = fetched.cache_path
    candidate.metadata["redirect_chain"] = fetched.redirect_chain


def _status(accepted: list[dict], rejected: list[dict], jury_rows: list[SourceJuryAssessment]) -> str:
    if accepted and any(row.get("jury", {}).get("recommended_use") == "binding" for row in accepted):
        return "authoritative_sources_found"
    if accepted:
        return "supporting_sources_only"
    if rejected:
        return "sources_rejected"
    if jury_rows:
        return "no_candidates"
    return "not_found"


def _canonical(url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    query = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
    blocked = {"utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content", "fbclid", "gclid", "mc_cid", "mc_eid"}
    kept = [(key, value) for key, value in query if key.lower() not in blocked]
    return urllib.parse.urlunparse((parsed.scheme.lower(), parsed.netloc.lower(), parsed.path, "", urllib.parse.urlencode(kept), ""))
