from __future__ import annotations

from pathlib import Path

from ralfloop_agent.domains.bandi_registry import (
    BandoDocument,
    BandoIdentity,
    BandoRegistry,
    BandoRule,
    BandoVersion,
)


def _version(bando_id: str, version: str, rules: list[BandoRule]) -> BandoVersion:
    return BandoVersion(
        identity=BandoIdentity(bando_id=bando_id, title=f"Bando {bando_id}", issuer="Regione", edition="2026", territory="IT"),
        version=version,
        status="draft",
        publication_date="2026-01-01",
        official_sources=[BandoDocument("call", "Bando", "official_call_text", "memory://call", "2026-01-01", "fixture")],
        rules=rules,
    )


def test_two_bandi_do_not_share_rules(tmp_path: Path):
    registry = BandoRegistry(tmp_path / "domains")
    registry.register_draft(_version("a", "1.0.0", [BandoRule("contribution", "contribution", 1000, "call")]))
    registry.register_draft(_version("b", "1.0.0", [BandoRule("contribution", "contribution", 2000, "call")]))

    assert registry.evaluate({"bando_id": "a", "field": "contribution"}).result["value"] == 1000
    assert registry.evaluate({"bando_id": "b", "field": "contribution"}).result["value"] == 2000


def test_same_edition_two_versions_resolve_latest(tmp_path: Path):
    registry = BandoRegistry(tmp_path / "domains")
    registry.register_draft(_version("ponte", "1.0.0", [BandoRule("cap", "cap", 1000, "call")]))
    registry.register_draft(_version("ponte", "1.1.0", [BandoRule("cap", "cap", 1500, "call")]))

    version = registry.get_version("ponte")
    assert version.version == "1.1.0"
    assert registry.resolve_bando("calcola ponte").version == "1.1.0"


def test_verify_sources_reports_missing_absolute_file(tmp_path: Path):
    registry = BandoRegistry(tmp_path / "domains")
    missing = "/tmp/ralfloop_missing_official_bando_source"
    registry.register_draft(
        BandoVersion(
            identity=BandoIdentity("x", "Bando X", "Regione", "2026"),
            version="1.0.0",
            status="draft",
            official_sources=[BandoDocument("call", "Bando", "official_call_text", missing, checksum="bad")],
        )
    )
    report = registry.verify_sources("x")
    assert report["status"] == "validation_required"
    assert missing in report["missing"]
