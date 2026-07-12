from ralfloop_agent.domains.bando_source_review import build_source_review


def test_binding_sets_do_not_cross_contaminate_issuers():
    act = build_source_review(
        bando_id="fondazione_unipolis_act_2026",
        version="1.0.0",
        research_result={
            "accepted_sources": [{"title": "ACT", "url": "https://www.fondazioneunipolis.org/a", "jury": {"recommended_use": "binding"}}],
            "rejected_sources": [],
        },
    ).source_update_proposal
    ponti = build_source_review(
        bando_id="fondazione_cariplo_nuovi_ponti_culturali_2026",
        version="1.0.0",
        research_result={
            "accepted_sources": [{"title": "Ponti", "url": "https://www.fondazionecariplo.it/a", "jury": {"recommended_use": "binding"}}],
            "rejected_sources": [],
        },
    ).source_update_proposal
    assert "fondazionecariplo" not in act["new_official_sources"][0]["url"]
    assert "fondazioneunipolis" not in ponti["new_official_sources"][0]["url"]
