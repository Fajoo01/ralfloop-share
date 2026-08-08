from ralfloop_agent.domains.bando_web_research import BandoWebResearcher, WebResearchRequest
from ralfloop_agent.domains.source_discovery import MockSearchProvider, SearchProviderUnavailable, SearchResultCandidate
from ralfloop_agent.domains.source_fetcher import FetchedSource


class FailingProvider:
    def search(self, query: str, *, limit: int):
        raise AssertionError("network should not be called")


class UnavailableProvider:
    def search(self, query: str, *, limit: int):
        raise SearchProviderUnavailable("missing")


class StubFetcher:
    def fetch(self, url: str):
        return FetchedSource(True, "fetched", url, final_url=url, content_type="application/pdf", checksum="abc", size_bytes=3)


def _official_result():
    return SearchResultCandidate(
        "c1",
        "",
        "Regolamento Bando ACT 2026",
        "https://www.fondazioneunipolis.org/bando-act-2026.pdf",
        publisher="Fondazione Unipolis",
        metadata={
            "checksum": "abc",
            "content_type": "application/pdf",
            "document_type": "primary_official_document",
            "publication_date": "2026-01-01",
            "direct_document": True,
            "skip_fetch": True,
        },
    )


def test_web_disabled_by_default_no_search(monkeypatch):
    monkeypatch.delenv("RALFLOOP_ENABLE_BANDO_WEB_RESEARCH", raising=False)
    request = WebResearchRequest(title="ACT 2026", issuer="Fondazione Unipolis")
    out = BandoWebResearcher(provider=FailingProvider()).research(request)
    assert out.status == "web_disabled"
    assert out.candidates == []


def test_provider_unavailable_with_opt_in():
    request = WebResearchRequest(title="ACT 2026", issuer="Fondazione Unipolis", allow_web=True)
    out = BandoWebResearcher(provider=UnavailableProvider()).research(request)
    assert out.status == "search_provider_unavailable"


def test_official_candidate_accepted_with_hard_gate():
    request = WebResearchRequest(title="ACT 2026", issuer="Fondazione Unipolis", allow_web=True)
    out = BandoWebResearcher(provider=MockSearchProvider([_official_result()]), fetcher=StubFetcher()).research(request)
    assert out.status == "authoritative_sources_found"
    assert out.accepted_sources
    assert out.accepted_sources[0]["jury"]["recommended_use"] == "binding"
    assert out.validation_required is True


def test_search_snippet_rejected():
    result = SearchResultCandidate("c1", "", "search snippet", "https://blog.example.org/x", snippet="search snippet")
    request = WebResearchRequest(title="ACT 2026", issuer="Fondazione Unipolis", allow_web=True)
    out = BandoWebResearcher(provider=MockSearchProvider([result]), fetcher=StubFetcher()).research(request)
    assert out.status == "sources_rejected"
    assert out.accepted_sources == []


def test_queries_include_missing_faq_and_amendment():
    request = WebResearchRequest(
        title="ACT 2026",
        issuer="Fondazione Unipolis",
        missing_documents=["official_faq", "official_amendment"],
        allow_web=True,
    )
    out = BandoWebResearcher(provider=MockSearchProvider([]), fetcher=StubFetcher()).research(request)
    joined = "\n".join(q["query"] for q in out.queries)
    assert "FAQ" in joined
    assert "rettifica" in joined
