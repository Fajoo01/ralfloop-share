from ralfloop_agent.domains.source_authority import SourceCandidate, assess_authority
from ralfloop_agent.domains.source_jury import SourceJury


def test_jury_cannot_override_failed_hard_gate():
    candidate = SourceCandidate(
        "c1",
        "https://www.fondazioneunipolis.org/bando-act-2026.pdf",
        "Regolamento Bando ACT 2026",
        "Fondazione Unipolis",
        document_type="primary_official_document",
    )
    authority = assess_authority(candidate, issuer="Fondazione Unipolis")
    jury = SourceJury().assess(candidate, authority)
    assert authority.deterministic_gate is False
    assert jury.binding_eligible is False
    assert jury.recommended_use != "binding"
    assert "jury_cannot_override_hard_gate" in jury.concerns


def test_jury_marks_official_binding_when_gate_passes():
    candidate = SourceCandidate(
        "c1",
        "https://www.fondazioneunipolis.org/bando-act-2026.pdf",
        "Regolamento Bando ACT 2026",
        "Fondazione Unipolis",
        document_type="primary_official_document",
        checksum="abc",
        content_type="application/pdf",
        publication_date="2026-01-01",
        direct_document=True,
    )
    authority = assess_authority(candidate, issuer="Fondazione Unipolis")
    jury = SourceJury().assess(candidate, authority)
    assert jury.official is True
    assert jury.recommended_use == "binding"
    assert jury.binding_eligible is True


def test_jury_detects_issuer_mismatch_and_supporting_limit():
    candidate = SourceCandidate(
        "c1",
        "https://www.partner.example.org/scheda.pdf",
        "Scheda bando",
        "Partner",
        document_type="unknown",
        checksum="abc",
        content_type="application/pdf",
        publication_date="2026-01-01",
    )
    authority = assess_authority(candidate, issuer="Fondazione Unipolis")
    jury = SourceJury().assess(candidate, authority)
    assert jury.recommended_use == "supporting"
    assert "issuer_mismatch" in jury.concerns
    assert "supporting_source_not_binding" in jury.concerns


def test_conflict_assessment_uses_checksums():
    jury = SourceJury()
    same = jury.assess_conflict({"checksum": "a"}, {"checksum": "a"})
    changed = jury.assess_conflict({"checksum": "a"}, {"checksum": "b"})
    assert same.status == "same_document"
    assert changed.status == "different_document"
    assert changed.human_review_required is True
