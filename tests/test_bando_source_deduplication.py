from ralfloop_agent.domains.bando_web_research import BandoWebResearcher, WebResearchRequest
from ralfloop_agent.domains.source_discovery import MockSearchProvider, SearchResultCandidate
from ralfloop_agent.domains.source_fetcher import FetchedSource


class SameContentFetcher:
    def fetch(self, url):
        return FetchedSource(True, "fetched", url, final_url=url, content_type="application/pdf", checksum="same", size_bytes=1)


def test_duplicate_urls_and_content_are_deduplicated():
    rows = [
        SearchResultCandidate("a", "q", "Regolamento ACT 2026", "https://www.fondazioneunipolis.org/doc.pdf?utm_source=x", publisher="Fondazione Unipolis"),
        SearchResultCandidate("b", "q", "Regolamento ACT 2026", "https://www.fondazioneunipolis.org/doc.pdf", publisher="Fondazione Unipolis"),
        SearchResultCandidate("c", "q", "Regolamento ACT 2026 copia", "https://www.fondazioneunipolis.org/doc-copy.pdf", publisher="Fondazione Unipolis"),
    ]
    out = BandoWebResearcher(provider=MockSearchProvider(rows), fetcher=SameContentFetcher()).research(
        WebResearchRequest(title="Bando ACT 2026", issuer="Fondazione Unipolis", queries=["q"], allow_web=True)
    )
    assert out.duplicate_count >= 2
    assert out.duplicate_urls
    assert out.duplicate_content
    assert out.binding_accepted_count == 1
