from pathlib import Path

from ralfloop_agent.domains.bando_domain_builder import BandoDomainBuilder
from ralfloop_agent.domains.source_discovery import MockSearchProvider, SearchResultCandidate


ACT = "fondazione_unipolis_act_2026"


def test_no_network_without_opt_in_with_real_pipeline(monkeypatch):
    monkeypatch.delenv("RALFLOOP_ENABLE_BANDO_WEB_RESEARCH", raising=False)
    out = BandoDomainBuilder().research_web(ACT, "1.0.0", provider=MockSearchProvider([SearchResultCandidate("c", "", "x", "https://x.test")]))
    assert out["status"] == "web_disabled"
    assert out["accepted_sources"] == []


def test_real_pipeline_rejects_hard_gate_failure():
    result = SearchResultCandidate(
        "c1",
        "",
        "Regolamento ACT 2026",
        "https://www.fondazioneunipolis.org/regolamento-act.pdf",
        publisher="Fondazione Unipolis",
    )
    out = BandoDomainBuilder().research_web(ACT, "1.0.0", allow_web=True, provider=MockSearchProvider([result]))
    assert out["accepted_sources"] == []
    assert out["rejected_sources"][0]["authority"]["deterministic_gate"] is False


def test_real_pipeline_no_auto_promotion():
    result = SearchResultCandidate(
        "c1",
        "",
        "FAQ ACT 2026",
        "https://www.fondazioneunipolis.org/faq-act.pdf",
        publisher="Fondazione Unipolis",
        metadata={"checksum": "abc", "content_type": "application/pdf", "document_type": "official_faq", "publication_date": "2026-01-01", "direct_document": True, "skip_fetch": True},
    )
    out = BandoDomainBuilder().rebuild_with_discovered_sources(ACT, "1.0.0", allow_web=True, provider=MockSearchProvider([result]))
    assert out["status"] == "validation_required"
    assert out["active_modified"] is False
    assert not Path("domains/active/bandi/fondazione_unipolis_act_2026/1.0.0").exists()
