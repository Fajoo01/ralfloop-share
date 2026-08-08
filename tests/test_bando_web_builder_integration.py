from pathlib import Path

from ralfloop_agent.domains.bando_domain_builder import BandoDomainBuilder
from ralfloop_agent.domains.source_discovery import MockSearchProvider, SearchResultCandidate


ACT = "fondazione_unipolis_act_2026"


def _official_result():
    return SearchResultCandidate(
        "c1",
        "",
        "FAQ ufficiale ACT 2026",
        "https://www.fondazioneunipolis.org/faq-act-2026.pdf",
        publisher="Fondazione Unipolis",
        metadata={
            "checksum": "faq-checksum",
            "content_type": "application/pdf",
            "document_type": "official_faq",
            "publication_date": "2026-02-01",
            "direct_document": True,
            "skip_fetch": True,
        },
    )


def test_builder_discovers_missing_documents():
    out = BandoDomainBuilder().discover_missing_sources(ACT, "1.0.0")
    assert out["status"] == "ok"
    kinds = {item["document_type"] for item in out["missing_documents"]}
    assert "official_faq" in kinds
    assert "official_reporting_manual" in kinds


def test_builder_research_web_disabled():
    out = BandoDomainBuilder().research_web(ACT, "1.0.0")
    assert out["status"] == "web_disabled"
    assert out["missing_documents"]


def test_builder_research_accepts_official_source():
    out = BandoDomainBuilder().research_web(ACT, "1.0.0", allow_web=True, provider=MockSearchProvider([_official_result()]))
    assert out["status"] == "authoritative_sources_found"
    assert out["accepted_sources"]
    assert out["human_review_required"] is True


def test_rebuild_with_discovered_sources_does_not_promote():
    out = BandoDomainBuilder().rebuild_with_discovered_sources(ACT, "1.0.0", allow_web=True, provider=MockSearchProvider([_official_result()]))
    assert out["status"] == "validation_required"
    assert out["active_modified"] is False
    assert not Path("domains/active/bandi/fondazione_unipolis_act_2026/1.0.0").exists()


def test_assess_source_candidates_keeps_supporting_non_binding():
    candidate = {
        "candidate_id": "c1",
        "url": "https://www.partner.example.org/scheda.pdf",
        "title": "Scheda",
        "publisher": "Partner",
        "document_type": "unknown",
        "content_type": "application/pdf",
        "checksum": "abc",
        "publication_date": "2026-01-01",
    }
    out = BandoDomainBuilder().assess_source_candidates([candidate], issuer="Fondazione Unipolis")
    row = out["assessments"][0]
    assert row["authority"]["authority_level"] == "C"
    assert row["jury"]["recommended_use"] == "supporting"
