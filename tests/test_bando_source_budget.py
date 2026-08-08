from ralfloop_agent.domains.bando_web_research import BandoWebResearcher, WebResearchRequest
from ralfloop_agent.domains.source_discovery import MockSearchProvider, SearchResultCandidate
from ralfloop_agent.domains.source_fetcher import FetchedSource


class CountingFetcher:
    def __init__(self):
        self.calls = []

    def fetch(self, url):
        self.calls.append(url)
        return FetchedSource(True, "fetched", url, final_url=url, content_type="application/pdf", checksum=f"sha-{len(self.calls)}", size_bytes=1)


def test_max_full_fetch_budget_is_respected():
    rows = [
        SearchResultCandidate(
            f"c{i}",
            "q",
            f"Regolamento ACT 2026 {i}",
            f"https://www.fondazioneunipolis.org/doc-{i}.pdf",
            publisher="Fondazione Unipolis",
        )
        for i in range(12)
    ]
    fetcher = CountingFetcher()
    out = BandoWebResearcher(provider=MockSearchProvider(rows), fetcher=fetcher).research(
        WebResearchRequest(
            bando_id="fondazione_unipolis_act_2026",
            title="Bando ACT 2026",
            issuer="Fondazione Unipolis",
            queries=["q"],
            allow_web=True,
            max_results_per_query=12,
            max_full_fetches=3,
            max_official_fetches=3,
        )
    )
    assert len(fetcher.calls) == 3
    assert len(out.downloads) == 3
    assert out.not_fetched_count == 9
    assert out.budget_exhausted is True
