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


def test_native_source_jury_vram_aware_eight_candidates():
    if os.getenv("RALFLOOP_BANDO_SOURCE_JURY_NATIVE_TEST") != "1":
        pytest.skip("native RecursiveMAS source jury test is opt-in")
    candidates = [
        {
            "candidate_id": "official_pdf",
            "title": "Regolamento ACT 2026",
            "url": "https://www.fondazioneunipolis.org/regolamento.pdf",
            "document_type": "primary_official_document",
            "content_type": "application/pdf",
            "checksum": "a" * 64,
        },
        {
            "candidate_id": "official_page",
            "title": "ACT Aspirare Coinvolgere Trasformare 2026",
            "url": "https://www.fondazioneunipolis.org/act-2026",
            "document_type": "official_archive_page",
            "content_type": "text/html",
            "checksum": "b" * 64,
        },
        {
            "candidate_id": "supporting_csv",
            "title": "Scheda informativa ACT 2026",
            "url": "https://www.csv.example/act-2026",
            "document_type": "unknown",
            "content_type": "text/html",
            "checksum": "c" * 64,
        },
        {
            "candidate_id": "blog_fail",
            "title": "Riassunto ACT 2026",
            "url": "https://blogspot.example/act",
            "document_type": "search_snippet",
            "content_type": "text/html",
            "checksum": "",
        },
    ] + [
        {
            "candidate_id": f"official_extra_{index}",
            "title": "Regolamento ACT 2026",
            "url": f"https://www.fondazioneunipolis.org/act-extra-{index}.pdf",
            "document_type": "primary_official_document",
            "content_type": "application/pdf",
            "checksum": f"{index}" * 64,
        }
        for index in range(4)
    ]

    out = assess_jury_sample(
        bando_id="fondazione_unipolis_act_2026",
        version="1.0.0",
        issuer="Fondazione Unipolis",
        recursive_mas=True,
        vram_aware=True,
        max_candidates=8,
        candidates=candidates,
    )

    assert out["candidate_count"] == 8
    assert out["completed_count"] == 8
    assert out["native_latent_verified_all_batches"] is True
    assert out["fallback"] is False
    assert out["native_result"]["batch_count"] >= 3
