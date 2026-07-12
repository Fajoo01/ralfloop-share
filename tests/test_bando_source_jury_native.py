import os

import pytest

from ralfloop_agent.domains.bando_source_review import assess_jury_sample


def test_native_source_jury_is_opt_in():
    if os.getenv("RALFLOOP_BANDO_SOURCE_JURY_NATIVE_TEST") != "1":
        pytest.skip("native RecursiveMAS source jury test is opt-in")
    out = assess_jury_sample(
        bando_id="fondazione_unipolis_act_2026",
        version="1.0.0",
        issuer="Fondazione Unipolis",
        recursive_mas=True,
        candidates=[
            {
                "candidate_id": "a",
                "title": "Regolamento ACT 2026",
                "url": "https://www.fondazioneunipolis.org/regolamento.pdf",
                "document_type": "primary_official_document",
                "content_type": "application/pdf",
                "checksum": "abc",
            }
        ],
    )
    assert out["jury_backend"] == "recursive_mas_native"
    assert out["fallback"] is False
