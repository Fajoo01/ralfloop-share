from ralfloop_agent.domains.bando_web_research import BandoWebResearcher, WebResearchRequest
from ralfloop_agent.domains.source_discovery import MockSearchProvider, SearchResultCandidate
from ralfloop_agent.domains.source_fetcher import FetchedSource


class StubFetcher:
    def fetch(self, url: str):
        return FetchedSource(True, "fetched", url, final_url=url, content_type="application/pdf", checksum="abc", size_bytes=3)


def test_searxng_result_official_after_metadata_gate():
    result = SearchResultCandidate(
        "c1",
        "",
        "Regolamento ACT 2026",
        "https://www.fondazioneunipolis.org/regolamento-act.pdf",
        publisher="Fondazione Unipolis",
        metadata={"provider": "searxng", "checksum": "abc", "content_type": "application/pdf", "document_type": "primary_official_document", "publication_date": "2026-01-01", "direct_document": True, "skip_fetch": True},
    )
    out = BandoWebResearcher(provider=MockSearchProvider([result]), fetcher=StubFetcher()).research(
        WebResearchRequest(title="ACT 2026", issuer="Fondazione Unipolis", allow_web=True)
    )
    assert out.status == "authoritative_sources_found"
    assert out.accepted_sources[0]["authority"]["authority_level"] == "A"


def test_snippet_only_not_binding_even_from_search_provider():
    result = SearchResultCandidate("c1", "", "ACT 2026 snippet", "https://www.fondazioneunipolis.org/page", snippet="snippet")
    out = BandoWebResearcher(provider=MockSearchProvider([result]), fetcher=StubFetcher()).research(
        WebResearchRequest(title="ACT 2026", issuer="Fondazione Unipolis", allow_web=True)
    )
    assert out.accepted_sources == []
    assert out.rejected_sources[0]["jury"]["recommended_use"] != "binding"


def test_supporting_c_can_discover_official_a_link():
    result = SearchResultCandidate(
        "c1",
        "",
        "Scheda ACT",
        "https://csv.example.org/act",
        publisher="CSV",
        metadata={
            "checksum": "support",
            "content_type": "text/html",
            "publication_date": "2026-01-01",
            "discovered_official_url": "https://www.fondazioneunipolis.org/regolamento-act.pdf",
            "discovered_official_title": "Regolamento ACT 2026",
            "discovered_document_type": "primary_official_document",
            "discovered_checksum": "official",
            "discovered_publication_date": "2026-01-02",
            "skip_fetch_discovered": True,
        },
    )
    out = BandoWebResearcher(provider=MockSearchProvider([result]), fetcher=StubFetcher()).research(
        WebResearchRequest(title="ACT 2026", issuer="Fondazione Unipolis", allow_web=True)
    )
    levels = [row["authority"]["authority_level"] for row in out.accepted_sources]
    assert "A" in levels
    assert "C" in levels
