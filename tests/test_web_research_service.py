from datetime import datetime, timezone

from ralfloop_agent.domains.source_discovery import MockSearchProvider, SearchResultCandidate
from ralfloop_agent.unified_assistant.memory_service import MemoryService
from ralfloop_agent.unified_assistant.web_research_mcp import TOOLS, WebResearchMCPServer
from ralfloop_agent.unified_assistant.web_research_service import (
    ClaimVerdict, OpenedWebSource, WebResearchService,
)


class FixtureOpener:
    def __init__(self, texts): self.texts = texts
    def open(self, url):
        return OpenedWebSource(url=url, title="Synthetic official source", text=self.texts[url], published_at=datetime(2026, 9, 1, tzinfo=timezone.utc))


def service(memory=None):
    urls = (
        "https://www.comune.milano.it/official-a",
        "https://example.org/secondary-b",
    )
    search = MockSearchProvider([
        SearchResultCandidate("a", "query", "Official", urls[0], "APS ammesse al bando", rank=1),
        SearchResultCandidate("b", "query", "Secondary", urls[1], "Commento secondario", rank=2),
    ])
    opener = FixtureOpener({
        urls[0]: "Le APS sono ammesse al bando. La scadenza è futura.",
        urls[1]: "Le APS non sono ammesse al bando secondo una fonte secondaria.",
    })
    return WebResearchService(search, opener, memory=memory)


def test_search_assigns_source_ids_and_authoritative_filter():
    rows = service().search_web("query")
    assert len(rows) == 2
    assert rows[0].source_id.startswith("research.")
    assert rows[0].authoritative is True
    assert rows[1].authoritative is False
    assert len(service().find_authoritative_source("query")) == 1


def test_open_persists_source_backed_evidence_in_memory(tmp_path):
    with MemoryService(tmp_path / "memory.sqlite") as memory:
        research = service(memory)
        source = research.search_web("query")[0]
        opened = research.open_source(source.source_id)
        assert opened.content_hash
        stored = memory.search_documents("scadenza")
        assert stored and stored[0].source.locator == str(source.url)


def test_compare_and_claim_verification_detect_conflict_and_no_evidence():
    research = service()
    sources = research.search_web("query")
    ids = tuple(row.source_id for row in sources)
    compared = research.compare_sources(ids, ("APS",))
    assert all(row.passages for row in compared)
    assert research.verify_claim("APS ammesse", ids).verdict is ClaimVerdict.CONFLICTING_SOURCES
    assert research.verify_claim("requisito inesistente", ids).verdict is ClaimVerdict.INSUFFICIENT_EVIDENCE


def test_research_mcp_has_six_semantic_tools_and_no_raw_url_open():
    research = service()
    server = WebResearchMCPServer(research)
    assert {row["name"] for row in server.list_tools()} == TOOLS
    found = server.call("research_search_web", {"query": "query"})
    source_id = found["structuredContent"]["data"][0]["source_id"]
    assert server.call("research_open_source", {"source_id": source_id})["structuredContent"]["data"]["content_hash"]
    assert server.call("research_open_source", {"source_id": source_id, "url": "http://127.0.0.1"})["isError"]
    assert not any("raw" in name or "crawl" in name for name in TOOLS)


def test_research_observability_counts_queries_sources_and_failures():
    research = service()
    research.search_web("query")
    metrics = research.metrics.snapshot()
    assert metrics["research_queries"] == 1
    assert metrics["research_sources"] == 2

    class FailedSearch:
        def search(self, query, limit=10):
            raise RuntimeError("offline")

    failed = WebResearchService(FailedSearch(), FixtureOpener({}))
    try:
        failed.search_web("query")
    except RuntimeError:
        pass
    assert failed.metrics.snapshot()["research_failures"] == 1
