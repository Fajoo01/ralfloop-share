from __future__ import annotations

from pathlib import Path

from openshell_backend.skills.abc_relcalc import calculate_relation_dict
from openshell_backend.skills import abc_formula_loop as afl
from ralfloop_agent.domains.capability_registry import CanonicalCapabilityRegistry
from ralfloop_agent.domains.deterministic_engine import DeterministicEngine


def test_abc_relcalc_adapter_matches_canonical_result():
    evidence = [
        {"kind": "observed_fact", "description": "Direct non-pressing invitation accepted", "weight": 18, "confidence": 0.9},
        {"kind": "observed_fact", "description": "Repeated practical trust gesture", "weight": 12, "confidence": 0.8},
    ]
    canonical = calculate_relation_dict(evidence)
    adapted = CanonicalCapabilityRegistry.from_mapping().execute("abc_relcalc", {"evidence": evidence})["result"]
    for key in ("score", "confidence", "bias_flags", "next_safe_action", "evidence_count", "summary"):
        assert adapted[key] == canonical[key]


def test_abc_formula_loop_adapter_matches_canonical_fields(tmp_path: Path):
    text = "Arianna propone due chiacchiere. Nama senza invitare Fabio."
    canonical = afl.score_text(
        text,
        memory_dir=tmp_path / "canonical_memory",
        evidence_cache_path=tmp_path / "canonical_evidence.json",
        force_extract=False,
    )
    adapted = CanonicalCapabilityRegistry.from_mapping().execute("abc_formula_loop", {"text": text})["result"]["raw"]
    for key in (
        "formula_version",
        "rlfull_current",
        "prudential_score",
        "relcalc_score",
        "confidence",
        "operative_range",
        "action",
        "bias_flags",
        "evidence_count",
    ):
        assert adapted[key] == canonical[key]


def test_domain_engine_runs_canonical_capability_before_jury():
    domain = {
        "manifest": {
            "domain_id": "abc_reasoning",
            "version": "1.0.0",
            "state": "active",
            "deterministic_capabilities": ["abc_relcalc"],
        }
    }
    result = DeterministicEngine().evaluate(
        domain,
        {
            "capability_id": "abc_relcalc",
            "inputs": {"evidence": [{"kind": "observed_fact", "description": "Warm signal", "weight": 10, "confidence": 0.8}]},
        },
    )
    assert result["complete"] is True
    assert result["capability_result"]["jury_required"] is False


def test_domain_engine_does_not_use_jury_for_missing_inputs():
    domain = {
        "manifest": {
            "domain_id": "abc_reasoning",
            "version": "1.0.0",
            "state": "active",
            "deterministic_capabilities": ["abc_relcalc"],
        }
    }
    result = DeterministicEngine().evaluate(domain, {"capability_id": "abc_relcalc", "inputs": {}})
    assert result["complete"] is False
    assert result["capability_result"]["status"] == "insufficient_input"
    assert result["capability_result"]["jury_required"] is False


def test_bandi_parity_covered_and_uncovered():
    registry = CanonicalCapabilityRegistry.from_mapping()
    context = {"bando_id": "demo", "official_rule_scope": "dimensioning"}
    covered = registry.execute("regione_act_dimensioning", {"query": "budget e contributo concesso", "bando_context": context})
    uncovered = registry.execute("regione_act_dimensioning", {"query": "regola non presente sulle merende", "bando_context": context})
    assert covered["status"] == "completed"
    assert covered["jury_required"] is False
    assert covered["result"]["applicability"]["subordinate_to_specific_bando"] is True
    assert uncovered["status"] == "uncovered_case"
    assert uncovered["jury_required"] is True
