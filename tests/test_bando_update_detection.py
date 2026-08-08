from ralfloop_agent.domains.bando_domain_builder import BandoDomainBuilder
from ralfloop_agent.domains.source_jury import SourceJury


def test_local_and_web_same_document_not_duplicated():
    out = BandoDomainBuilder().merge_source_sets(
        [{"source_id": "local", "checksum": "abc", "version": "1.0"}],
        [{"source_id": "web", "checksum": "abc", "version": "1.0"}],
    )
    assert len(out["sources"]) == 1
    assert out["conflicts"] == []


def test_web_new_checksum_requires_validation():
    out = BandoDomainBuilder().merge_source_sets(
        [{"source_id": "local", "checksum": "abc", "version": "1.0"}],
        [{"source_id": "web", "checksum": "def", "version": "1.0"}],
    )
    assert out["validation_required"] is True
    assert out["conflicts"][0]["status"] == "different_document"


def test_new_version_marked_for_human_review():
    conflict = SourceJury().assess_conflict({"checksum": "abc", "version": "1.0"}, {"checksum": "def", "version": "1.1"})
    assert conflict.status == "newer_official_version"
    assert conflict.human_review_required is True


def test_check_updates_web_disabled_by_default():
    out = BandoDomainBuilder().research_web("fondazione_unipolis_act_2026", "1.0.0")
    assert out["status"] == "web_disabled"
