from ralfloop_agent.domains.bando_web_research import BandoWebResearcher, WebResearchRequest
from ralfloop_agent.domains.source_discovery import MockSearchProvider, SearchResultCandidate
from ralfloop_agent.domains.source_fetcher import FetchedSource


class Fetcher:
    def fetch(self, url):
        return FetchedSource(True, "fetched", url, final_url=url, content_type="application/pdf", checksum=url, size_bytes=1)


def test_official_direct_document_ranks_before_supporting_page():
    rows = [
        SearchResultCandidate("c", "q", "Scheda ACT 2026", "https://csv.example.org/act", publisher="CSV"),
        SearchResultCandidate("a", "q", "Regolamento ACT 2026", "https://www.fondazioneunipolis.org/regolamento.pdf", publisher="Fondazione Unipolis"),
    ]
    out = BandoWebResearcher(provider=MockSearchProvider(rows), fetcher=Fetcher()).research(
        WebResearchRequest(title="Bando ACT 2026", issuer="Fondazione Unipolis", queries=["q"], allow_web=True, max_full_fetches=1, max_official_fetches=1, max_supporting_fetches=0)
    )
    assert out.binding_accepted_count == 1
    assert out.accepted_sources[0]["url"].startswith("https://www.fondazioneunipolis.org/")
