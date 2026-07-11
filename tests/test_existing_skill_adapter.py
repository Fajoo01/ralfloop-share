from __future__ import annotations

from ralfloop_agent.domains.capability_registry import CanonicalCapabilityRegistry


def test_abc_relcalc_complete_input_does_not_use_jury():
    registry = CanonicalCapabilityRegistry.from_mapping()
    out = registry.execute(
        "abc_relcalc",
        {
            "evidence": [
                {"kind": "observed_fact", "description": "Warm direct signal", "weight": 12, "confidence": 0.8}
            ]
        },
    )
    assert out["status"] == "completed"
    assert out["jury_required"] is False
    assert out["deterministic"] is True
    assert out["canonical_executor"] == "openshell_backend/skills/abc_relcalc.py"
    assert "score" in out["result"]


def test_abc_relcalc_missing_input_never_invokes_jury():
    out = CanonicalCapabilityRegistry.from_mapping().execute("abc_relcalc", {})
    assert out["status"] == "insufficient_input"
    assert out["jury_required"] is False
    assert out["unresolved_questions"] == ["evidence"]


def test_abc_formula_loop_calls_canonical_executor_without_jury():
    out = CanonicalCapabilityRegistry.from_mapping().execute("abc_formula_loop", {"text": "Arianna propone due chiacchiere."})
    assert out["ok"] is True
    assert out["result"]["formula_version"] == "abc_formula_loop_v1"
    assert out["jury_required"] is False
    assert out["canonical_executor"] == "openshell_backend/skills/abc_formula_loop.py"


def test_bandi_rulebook_covered_case_is_deterministic():
    out = CanonicalCapabilityRegistry.from_mapping().execute(
        "regione_act_dimensioning",
        {"query": "Come separare beneficiari diretti pubblico indiretti?", "bando_context": {"bando_id": "demo", "official_rule_scope": "dimensioning"}},
    )
    assert out["status"] == "completed"
    assert out["jury_required"] is False
    assert out["deterministic"] is True
    assert "beneficiari" in out["result"]["matched_sections"]
    assert out["result"]["applicability"]["capability_scope"] == "cross_bando"


def test_bandi_rulebook_uncovered_case_requires_jury_without_inventing():
    out = CanonicalCapabilityRegistry.from_mapping().execute(
        "regione_act_dimensioning",
        {"query": "Quale nuova regola inventiamo per un caso non scritto?", "bando_context": {"bando_id": "demo", "official_rule_scope": "dimensioning"}},
    )
    assert out["status"] == "uncovered_case"
    assert out["jury_required"] is True
    assert out["jury_reason_codes"] == ["incomplete_rules"]
    assert out["result"] == {}


def test_bandi_rulebook_conflict_is_explicit():
    out = CanonicalCapabilityRegistry.from_mapping().execute(
        "regione_act_dimensioning",
        {"query": "beneficiari", "simulate_conflict": True, "bando_context": {"bando_id": "demo", "official_rule_scope": "dimensioning"}},
    )
    assert out["status"] == "conflicting_rules"
    assert out["jury_required"] is True
    assert out["jury_reason_codes"] == ["conflicting_sources"]


def test_bandi_rulebook_without_bando_context_does_not_identify_bando():
    out = CanonicalCapabilityRegistry.from_mapping().execute(
        "regione_act_dimensioning",
        {"query": "Come separare beneficiari diretti pubblico indiretti?"},
    )
    assert out["status"] == "missing_context"
    assert out["jury_required"] is False
    assert "bando_id" in out["unresolved_questions"]
