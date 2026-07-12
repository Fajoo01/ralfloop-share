from ralfloop_agent.domains.source_authority import SourceCandidate, assess_authority
from ralfloop_agent.domains.source_jury import SourceJury


def test_calibrated_a_b_c_d_and_binding_limits():
    official_pdf = SourceCandidate("a", "https://www.fondazioneunipolis.org/regolamento.pdf", "Regolamento ACT 2026", "Fondazione Unipolis", document_type="primary_official_document", content_type="application/pdf", checksum="a", publication_date="2026-01-01", direct_document=True)
    faq = SourceCandidate("b", "https://www.fondazioneunipolis.org/faq.pdf", "FAQ ACT 2026", "Fondazione Unipolis", document_type="official_faq", content_type="application/pdf", checksum="b", publication_date="2026-01-02", direct_document=True)
    support = SourceCandidate("c", "https://csv.example.org/act-2026", "Scheda ACT 2026", "CSV", document_type="unknown", content_type="text/html", checksum="c", publication_date="2026-01-03", metadata={"expected_title": "Bando ACT 2026"})
    blog = SourceCandidate("d", "https://foo.blogspot.com/act", "ACT 2026 senza fonte", "", document_type="unknown", content_type="text/html", checksum="d", metadata={"expected_title": "Bando ACT 2026"})
    assert assess_authority(official_pdf, issuer="Fondazione Unipolis").authority_level == "A"
    assert assess_authority(faq, issuer="Fondazione Unipolis").authority_level == "B"
    c_auth = assess_authority(support, issuer="Fondazione Unipolis")
    d_auth = assess_authority(blog, issuer="Fondazione Unipolis")
    assert c_auth.authority_level == "C"
    assert SourceJury().assess(support, c_auth).recommended_use == "supporting"
    assert d_auth.authority_level == "D"
    assert SourceJury().assess(blog, d_auth).recommended_use == "reject"
