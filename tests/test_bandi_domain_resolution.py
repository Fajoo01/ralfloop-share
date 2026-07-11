from __future__ import annotations

from pathlib import Path

from ralfloop_agent.domains.bandi_registry import BandoIdentity, BandoRegistry, BandoRule, BandoVersion
from ralfloop_agent.domains.capability_registry import CanonicalCapabilityRegistry
from ralfloop_agent.domains.deterministic_engine import DeterministicEngine


def test_regione_act_dimensioning_does_not_identify_bando_by_itself():
    resolution = BandoRegistry().resolve_bando("usa regione act dimensioning")
    assert resolution.status == "missing_context"
    assert resolution.reason_codes == ["bando_not_identified"]


def test_bando_not_identified_requests_identification():
    result = BandoRegistry().evaluate({"field": "contribution"})
    assert result.status == "missing_context"
    assert result.jury_required is False


def test_meta_framework_not_used_as_operational_domain():
    domain = {"manifest": {"domain_id": "bandi_framework", "version": "1.0.0", "state": "active", "deterministic_capabilities": ["regione_act_dimensioning"]}}
    result = DeterministicEngine().evaluate(domain, {"capability_id": "regione_act_dimensioning", "inputs": {"query": "budget", "lookup_only": True}})
    assert result["complete"] is False
    assert result["unresolved_questions"] == ["meta_framework_not_operational"]


def test_rulebook_lookup_is_cross_bando_capability_not_bando_domain():
    cap = CanonicalCapabilityRegistry.from_mapping().get("regione_act_dimensioning")
    assert cap.domain_id == "bandi_framework"
    assert cap.domain_family == "bandi"
    assert cap.capability_scope == "cross_bando"
    assert cap.required_domain_context == ["bando_id", "official_rule_scope"]


def test_specific_bando_explicit_rule_is_deterministic(tmp_path: Path):
    registry = BandoRegistry(tmp_path / "domains")
    registry.register_draft(
        BandoVersion(
            identity=BandoIdentity("culture", "Ponti culturali", "Regione", "2026"),
            version="1.0.0",
            status="draft",
            rules=[BandoRule("deadline", "deadline", "2026-09-30", "call")],
        )
    )
    out = registry.evaluate({"bando_id": "culture", "field": "deadline"})
    assert out.status == "completed"
    assert out.deterministic is True
    assert out.jury_required is False
    assert out.result["value"] == "2026-09-30"
