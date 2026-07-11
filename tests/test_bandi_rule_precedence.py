from __future__ import annotations

from pathlib import Path

from ralfloop_agent.domains.bandi_registry import (
    BandoConflict,
    BandoDocument,
    BandoIdentity,
    BandoRegistry,
    BandoRule,
    BandoVersion,
)


def test_later_official_correction_prevails(tmp_path: Path):
    registry = BandoRegistry(tmp_path / "domains")
    registry.register_draft(
        BandoVersion(
            identity=BandoIdentity("act", "ACT", "Regione", "2026"),
            version="1.0.0",
            status="draft",
            official_sources=[BandoDocument("call", "Bando", "official_call_text", "memory://call")],
            amendments=[BandoDocument("corr", "Rettifica", "official_correction_later", "memory://corr", "2026-02-01")],
            rules=[
                BandoRule("percentage", "percentage", 50, "call", "official_call_text", effective_from="2026-01-01"),
                BandoRule("percentage", "percentage", 60, "corr", "official_correction_later", effective_from="2026-02-01"),
            ],
        )
    )
    out = registry.evaluate({"bando_id": "act", "field": "percentage"})
    assert out.status == "completed"
    assert out.result["value"] == 60
    assert out.source_refs == ["corr"]


def test_faq_conflict_is_reported_not_invented(tmp_path: Path):
    registry = BandoRegistry(tmp_path / "domains")
    registry.register_draft(
        BandoVersion(
            identity=BandoIdentity("act", "ACT", "Regione", "2026"),
            version="1.0.0",
            status="draft",
            faq=[BandoDocument("faq1", "FAQ", "official_faq_later", "memory://faq")],
            rules=[
                BandoRule("cap_a", "cap", 1000, "faq1", "official_faq_later"),
                BandoRule("cap_b", "cap", 2000, "faq1", "official_faq_later"),
            ],
            conflicts=[BandoConflict("faq_cap_conflict", "cap", ["faq1"], "FAQ gives incompatible caps")],
        )
    )
    out = registry.evaluate({"bando_id": "act", "field": "cap"})
    assert out.status == "conflicting_official_documents"
    assert out.jury_required is True
    assert out.jury_reason_codes == ["conflicting_sources"]


def test_uncovered_case_does_not_add_rule(tmp_path: Path):
    registry = BandoRegistry(tmp_path / "domains")
    registry.register_draft(
        BandoVersion(
            identity=BandoIdentity("act", "ACT", "Regione", "2026"),
            version="1.0.0",
            status="draft",
            rules=[BandoRule("deadline", "deadline", "2026-09-30", "call")],
        )
    )
    out = registry.evaluate({"bando_id": "act", "field": "catering"})
    assert out.status == "uncovered_case"
    assert out.result is None
    assert out.jury_required is True
    assert out.jury_reason_codes == ["incomplete_rules"]
