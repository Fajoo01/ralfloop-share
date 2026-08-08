from __future__ import annotations

from ralfloop_agent.domains.deterministic_engine import DeterministicEngine


def test_domain_engine_calls_calculation_orchestrator_before_jury():
    domain = {"manifest": {"domain_id": "bandi/demo", "version": "1.0.0", "state": "active", "deterministic_capabilities": ["calculation_orchestrator"]}}
    out = DeterministicEngine().evaluate(domain, {"calculation": {"expression": "20% di 10000"}})
    assert out["complete"] is True
    assert out["calculation_result"]["answer"] == "2000"
    assert out["calculation_result"]["jury_required"] is False


def test_domain_engine_does_not_use_jury_for_missing_numeric_input():
    domain = {"manifest": {"domain_id": "bandi/demo", "version": "1.0.0", "state": "active", "deterministic_capabilities": ["calculation_orchestrator"]}}
    out = DeterministicEngine().evaluate(domain, {"calculation": {"expression": "20% di"}})
    assert out["complete"] is False
    assert out["calculation_result"]["status"] == "insufficient_input"
    assert out["calculation_result"]["jury_required"] is False


def test_domain_engine_scenarios_are_residue_for_jury_only_after_calculation():
    domain = {"manifest": {"domain_id": "bandi/demo", "version": "1.0.0", "state": "active", "deterministic_capabilities": ["calculation_orchestrator"]}}
    out = DeterministicEngine().evaluate(
        domain,
        {
            "calculation": {
                "scenarios": [
                    {"interpretation": "totale", "base": 10000, "percent": 20},
                    {"interpretation": "ammissibile", "base": 8000, "percent": 20},
                ]
            }
        },
    )
    assert out["complete"] is False
    assert out["calculation_result"]["status"] == "scenario_result"
    assert len(out["calculation_result"]["scenarios"]) == 2
