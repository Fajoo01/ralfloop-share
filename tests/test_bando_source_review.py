import json

from ralfloop_agent.domains.bando_source_review import assess_jury_sample, build_source_review


def _research():
    return {
        "bando_id": "fondazione_unipolis_act_2026",
        "version": "1.0.0",
        "candidates": [{"candidate_id": "a", "title": "Regolamento ACT", "url": "https://www.fondazioneunipolis.org/a.pdf"}],
        "accepted_sources": [
            {"candidate_id": "a", "title": "Regolamento ACT", "url": "https://www.fondazioneunipolis.org/a.pdf", "jury": {"recommended_use": "binding", "authority_level": "A"}, "fetch": {"checksum": "abc"}}
        ],
        "rejected_sources": [
            {"candidate_id": "d", "title": "blog", "url": "https://x.blogspot.com/a", "jury": {"recommended_use": "reject", "authority_level": "D"}}
        ],
        "downloads": [{"checksum": "abc", "cache_path": "/tmp/x"}],
        "human_review_required": True,
    }


def test_source_review_writes_dossier(tmp_path):
    summary = build_source_review(bando_id="fondazione_unipolis_act_2026", version="1.0.0", research_result=_research(), output_dir=tmp_path)
    assert summary.binding_count == 1
    assert summary.rejected_count == 1
    assert (tmp_path / "binding-sources.json").exists()
    assert json.loads((tmp_path / "source_update_proposal.json").read_text())["domain_promoted"] is False


def test_jury_sample_hard_gate_failure_stays_not_binding():
    out = assess_jury_sample(
        bando_id="fondazione_unipolis_act_2026",
        version="1.0.0",
        issuer="Fondazione Unipolis",
        candidates=[{"candidate_id": "x", "title": "snippet", "url": "https://x.blogspot.com/a", "document_type": "search_snippet"}],
    )
    final = out["deterministic_results"][0]["final_assessment"]
    assert final["binding_eligible"] is False
    assert final["recommended_use"] == "reject"
