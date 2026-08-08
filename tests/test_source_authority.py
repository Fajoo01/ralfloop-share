from ralfloop_agent.domains.source_authority import SourceCandidate, assess_authority


def test_official_primary_domain_classified_a():
    candidate = SourceCandidate(
        "c1",
        "https://www.fondazioneunipolis.org/bando-act-2026.pdf",
        "Regolamento Bando ACT 2026",
        "Fondazione Unipolis",
        document_type="primary_official_document",
        content_type="application/pdf",
        checksum="abc",
        publication_date="2026-01-01",
        direct_document=True,
    )
    out = assess_authority(candidate, issuer="Fondazione Unipolis")
    assert out.authority_level == "A"
    assert out.binding_eligible is True


def test_official_faq_classified_b():
    candidate = SourceCandidate(
        "c1",
        "https://www.fondazionecariplo.it/faq-nuovi-ponti.pdf",
        "FAQ Nuovi Ponti",
        "Fondazione Cariplo",
        document_type="official_faq",
        content_type="application/pdf",
        checksum="abc",
        publication_date="2026-01-01",
        direct_document=True,
    )
    out = assess_authority(candidate, issuer="Fondazione Cariplo")
    assert out.authority_level == "B"
    assert out.binding_eligible is True


def test_supporting_portal_is_c_not_binding():
    candidate = SourceCandidate(
        "c1",
        "https://www.csv.example.org/scheda-bando",
        "Scheda bando ACT",
        "CSV Example",
        document_type="unknown",
        content_type="text/html",
        checksum="abc",
        publication_date="2026-01-01",
    )
    out = assess_authority(candidate, issuer="Fondazione Unipolis")
    assert out.authority_level == "C"
    assert out.binding_eligible is False


def test_search_snippet_is_d_or_not_binding():
    candidate = SourceCandidate(
        "c1",
        "https://blog.example.org/post",
        "search snippet riassunto",
        "",
        document_type="search_snippet",
        snippet="search snippet",
    )
    out = assess_authority(candidate, issuer="Fondazione Unipolis")
    assert out.binding_eligible is False
    assert "hard_gate_failed" in out.concerns
