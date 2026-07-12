from __future__ import annotations

import json
import os
import re
import time
import urllib.parse
import urllib.error
import urllib.request
import hashlib
from dataclasses import asdict, dataclass, field
from typing import Protocol


@dataclass
class SearchQuery:
    query: str
    purpose: str = "general"

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class SearchResultCandidate:
    candidate_id: str
    query: str
    title: str
    url: str
    snippet: str = ""
    publisher: str = ""
    rank: int = 0
    metadata: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


class SearchProvider(Protocol):
    def search(self, query: str, *, limit: int) -> list[SearchResultCandidate]:
        ...


class SearchProviderUnavailable(RuntimeError):
    pass


class MockSearchProvider:
    def __init__(self, results: list[SearchResultCandidate] | None = None) -> None:
        self.results = results or []
        self.queries: list[str] = []

    def search(self, query: str, *, limit: int) -> list[SearchResultCandidate]:
        self.queries.append(query)
        return [item for item in self.results if not item.query or item.query == query][:limit]


class ConfiguredSearchProvider:
    """Configurable provider. No commercial service dependency or credentials."""

    def __init__(self, endpoint: str | None = None, results_json: str | None = None, timeout_sec: float = 30.0, provider: str | None = None) -> None:
        self.endpoint = endpoint or os.getenv("RALFLOOP_BANDO_SEARCH_ENDPOINT")
        self.results_json = results_json or os.getenv("RALFLOOP_BANDO_SEARCH_RESULTS_JSON")
        self.timeout_sec = timeout_sec
        self.provider = (provider or os.getenv("RALFLOOP_BANDO_SEARCH_PROVIDER") or "none").strip().lower()
        self._delegate: SearchProvider | None = None
        self._delegate_error: str | None = None
        if not self.results_json and not self.endpoint:
            try:
                self._delegate = create_search_provider(self.provider)
            except SearchProviderUnavailable as exc:
                self._delegate_error = str(exc) or "search_provider_unavailable"

    @classmethod
    def from_env(cls) -> "ConfiguredSearchProvider":
        return cls(
            timeout_sec=float(os.getenv("RALFLOOP_SEARXNG_TIMEOUT_SEC", os.getenv("RALFLOOP_BANDO_WEB_TIMEOUT_SEC", "20"))),
            provider=os.getenv("RALFLOOP_BANDO_SEARCH_PROVIDER"),
        )

    def search(self, query: str, *, limit: int) -> list[SearchResultCandidate]:
        if self._delegate_error:
            raise SearchProviderUnavailable(self._delegate_error)
        if self._delegate is not None:
            return self._delegate.search(query, limit=limit)
        if self.results_json:
            rows = json.loads(self.results_json)
            return [_candidate_from_row(query, idx, row) for idx, row in enumerate(rows, 1)][:limit]
        if not self.endpoint:
            raise SearchProviderUnavailable("search_provider_unavailable")
        url = self.endpoint.format(query=urllib.parse.quote(query), limit=limit)
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=self.timeout_sec) as resp:
            rows = json.loads(resp.read().decode("utf-8"))
        if isinstance(rows, dict):
            rows = rows.get("results", [])
        return [_candidate_from_row(query, idx, row) for idx, row in enumerate(rows, 1)][:limit]

    def health(self) -> dict:
        if self._delegate is not None and hasattr(self._delegate, "health"):
            return self._delegate.health()  # type: ignore[attr-defined]
        if self._delegate_error:
            return {"provider": self.provider, "configured": False, "reachable": False, "json_enabled": False, "base_url": None, "latency_ms": 0, "error": self._delegate_error}
        return {"provider": "configured_endpoint", "configured": bool(self.endpoint or self.results_json), "reachable": None, "json_enabled": None, "base_url": self.endpoint, "latency_ms": 0, "error": None}


class SearxngSearchProvider:
    def __init__(
        self,
        base_url: str | None = None,
        *,
        timeout_sec: float | None = None,
        language: str | None = None,
        safesearch: str | int | None = None,
        categories: str | None = None,
        max_pages: int | None = None,
    ) -> None:
        self.base_url = (base_url or os.getenv("RALFLOOP_SEARXNG_URL") or "http://127.0.0.1:8888").rstrip("/")
        self.timeout_sec = float(timeout_sec if timeout_sec is not None else os.getenv("RALFLOOP_SEARXNG_TIMEOUT_SEC", "20"))
        self.language = language or os.getenv("RALFLOOP_SEARXNG_LANGUAGE", "it-IT")
        self.safesearch = str(safesearch if safesearch is not None else os.getenv("RALFLOOP_SEARXNG_SAFESEARCH", "1"))
        self.categories = categories or os.getenv("RALFLOOP_SEARXNG_CATEGORIES", "general")
        self.max_pages = int(max_pages if max_pages is not None else os.getenv("RALFLOOP_SEARXNG_MAX_PAGES", "1"))
        _validate_provider_url(self.base_url)

    def search(self, query: str, *, limit: int) -> list[SearchResultCandidate]:
        rows: list[dict] = []
        for page in range(1, max(1, self.max_pages) + 1):
            params = {
                "q": query,
                "format": "json",
                "language": self.language,
                "safesearch": self.safesearch,
                "categories": self.categories,
                "pageno": str(page),
            }
            url = f"{self.base_url}/search?{urllib.parse.urlencode(params)}"
            req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "Ralfloop-BandoResearch/1.0"})
            try:
                with urllib.request.urlopen(req, timeout=self.timeout_sec) as resp:
                    body = resp.read().decode("utf-8", errors="replace")
            except urllib.error.HTTPError as exc:
                if exc.code == 403:
                    raise SearchProviderUnavailable("provider_json_format_disabled") from exc
                if exc.code == 429:
                    raise SearchProviderUnavailable("provider_rate_limited") from exc
                raise SearchProviderUnavailable("provider_http_error") from exc
            except TimeoutError as exc:
                raise SearchProviderUnavailable("provider_timeout") from exc
            except urllib.error.URLError as exc:
                reason = getattr(exc, "reason", None)
                if isinstance(reason, TimeoutError):
                    raise SearchProviderUnavailable("provider_timeout") from exc
                raise SearchProviderUnavailable("provider_unavailable") from exc
            except (ConnectionError, OSError) as exc:
                raise SearchProviderUnavailable("provider_unavailable") from exc
            try:
                data = json.loads(body)
            except json.JSONDecodeError as exc:
                raise SearchProviderUnavailable("provider_invalid_json") from exc
            page_rows = data.get("results") if isinstance(data, dict) else None
            if page_rows is None:
                raise SearchProviderUnavailable("provider_invalid_json")
            if not page_rows and page == 1:
                raise SearchProviderUnavailable("provider_empty_result")
            rows.extend(row for row in page_rows if isinstance(row, dict))
            if len(rows) >= limit:
                break
        return _parse_searxng_results(query, rows, limit)

    def health(self) -> dict:
        started = time.monotonic()
        out = {"provider": "searxng", "configured": True, "reachable": False, "json_enabled": False, "base_url": self.base_url, "latency_ms": 0, "error": None}
        try:
            self.search("health", limit=1)
            out["reachable"] = True
            out["json_enabled"] = True
        except SearchProviderUnavailable as exc:
            out["error"] = str(exc)
            out["reachable"] = str(exc) not in {"provider_unavailable", "provider_timeout"}
            out["json_enabled"] = str(exc) != "provider_json_format_disabled"
        finally:
            out["latency_ms"] = int((time.monotonic() - started) * 1000)
        return out


def create_search_provider(provider: str | None = None) -> SearchProvider:
    name = (provider or os.getenv("RALFLOOP_BANDO_SEARCH_PROVIDER") or "none").strip().lower()
    if name in {"", "none"}:
        raise SearchProviderUnavailable("search_provider_unavailable")
    if name == "mock":
        return MockSearchProvider([])
    if name == "searxng":
        return SearxngSearchProvider()
    raise SearchProviderUnavailable("unsupported_search_provider")


def _compact_title_terms(title: str) -> str:
    title = re.sub(r"\s+", " ", title).strip()
    if not title:
        return ""
    head = re.split(r"\s+-\s+|:|;", title, maxsplit=1)[0].strip()
    return head or title


def generate_bando_queries(*, title: str = "", issuer: str = "", edition: str = "", territory: str = "", official_domain: str = "") -> list[SearchQuery]:
    core = _compact_title_terms(title)
    base = " ".join(part for part in (core or title, issuer, edition, territory) if part).strip()
    search_title = core or title
    queries = []
    if official_domain:
        queries.extend(
            [
                SearchQuery(f"site:{official_domain} {search_title}", "official_archive_page"),
                SearchQuery(f"site:{official_domain} {search_title} FAQ", "official_faq"),
                SearchQuery(f"site:{official_domain} {search_title} rettifica", "official_amendment"),
                SearchQuery(f"site:{official_domain} {search_title} proroga", "official_deadline_extension"),
                SearchQuery(f"site:{official_domain} {search_title} rendicontazione", "official_reporting_manual"),
            ]
        )
    if base:
        queries.append(SearchQuery(base, "general"))
    if search_title:
        queries.extend(
            [
                SearchQuery(f"{search_title} {issuer} regolamento", "primary_official_document"),
                SearchQuery(f"{search_title} {issuer} bando PDF", "primary_official_document"),
                SearchQuery(f"{search_title} FAQ", "official_faq"),
                SearchQuery(f"{search_title} rettifica", "official_amendment"),
                SearchQuery(f"{search_title} allegati", "official_attachment"),
                SearchQuery(f"{search_title} formulario", "official_application_form"),
                SearchQuery(f"{search_title} budget", "official_budget_template"),
                SearchQuery(f"{search_title} rendicontazione", "official_reporting_manual"),
            ]
        )
    if title and title != search_title:
        queries.append(SearchQuery(f'"{title}" regolamento', "primary_official_document"))
    seen = set()
    out = []
    for query in queries:
        if query.query not in seen:
            seen.add(query.query)
            out.append(query)
    return out


def canonicalize_url(url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    query = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
    blocked = {"utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content", "fbclid", "gclid", "mc_cid", "mc_eid"}
    kept = [(k, v) for k, v in query if k.lower() not in blocked]
    netloc = parsed.netloc.lower()
    scheme = parsed.scheme.lower()
    return urllib.parse.urlunparse((scheme, netloc, parsed.path, "", urllib.parse.urlencode(kept), ""))


def _candidate_from_row(query: str, idx: int, row: dict) -> SearchResultCandidate:
    return SearchResultCandidate(
        candidate_id=str(row.get("candidate_id") or f"candidate:{idx}"),
        query=str(row.get("query") or query),
        title=str(row.get("title") or ""),
        url=str(row.get("url") or ""),
        snippet=str(row.get("snippet") or ""),
        publisher=str(row.get("publisher") or ""),
        rank=int(row.get("rank") or idx),
        metadata=dict(row.get("metadata") or {}),
    )


def _parse_searxng_results(query: str, rows: list[dict], limit: int) -> list[SearchResultCandidate]:
    seen = set()
    out: list[SearchResultCandidate] = []
    retrieved_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    for idx, row in enumerate(rows, 1):
        url = str(row.get("url") or "")
        if not url:
            continue
        canonical = canonicalize_url(url)
        if canonical in seen:
            continue
        seen.add(canonical)
        title = str(row.get("title") or "")
        metadata = {
            "engine": row.get("engine"),
            "engines": row.get("engines") or ([row.get("engine")] if row.get("engine") else []),
            "category": row.get("category"),
            "published_at": row.get("publishedDate") or row.get("published_at"),
            "score": row.get("score"),
            "provider": "searxng",
            "retrieved_at": retrieved_at,
            "canonical_url": canonical,
        }
        out.append(
            SearchResultCandidate(
                candidate_id="searxng:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16],
                query=query,
                title=title,
                url=canonical,
                snippet=str(row.get("content") or row.get("snippet") or ""),
                publisher=str(row.get("engine") or ""),
                rank=len(out) + 1,
                metadata=metadata,
            )
        )
        if len(out) >= limit:
            break
    return out


def _validate_provider_url(base_url: str) -> None:
    parsed = urllib.parse.urlparse(base_url)
    if parsed.scheme != "http":
        raise SearchProviderUnavailable("provider_endpoint_not_allowlisted")
    if parsed.hostname not in {"127.0.0.1", "localhost"}:
        allowed = {item.strip() for item in os.getenv("RALFLOOP_SEARXNG_ALLOWED_HOSTS", "").split(",") if item.strip()}
        if parsed.hostname not in allowed:
            raise SearchProviderUnavailable("provider_endpoint_not_allowlisted")
