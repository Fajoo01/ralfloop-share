from __future__ import annotations

from ralfloop_agent.domains.calculation_orchestrator import CalculationOrchestrator


def test_percent_of_is_deterministic_without_jury():
    out = CalculationOrchestrator().calculate("20% di 10000")
    assert out["status"] == "completed"
    assert out["answer"] == "2000"
    assert out["deterministic"] is True
    assert out["jury_required"] is False


def test_explicit_formula_is_deterministic_complete():
    out = CalculationOrchestrator().calculate({"formula": "base * percent / 100", "values": {"base": 10000, "percent": 20}})
    assert out["status"] == "completed"
    assert out["answer"] == "2000"
    assert out["jury_required"] is False


def test_contribution_without_bando_context_does_not_invent_result():
    out = CalculationOrchestrator().calculate({"goal": "Calcola il contributo previsto dal bando"})
    assert out["status"] == "missing_context"
    assert out["answer"] is None
    assert out["jury_reason_codes"] == ["missing_context"]


def test_two_plausible_bases_return_two_deterministic_scenarios():
    out = CalculationOrchestrator().calculate(
        {
            "scenarios": [
                {"interpretation": "base costo totale", "base": 10000, "percent": 20, "source_refs": ["call:A"]},
                {"interpretation": "base spesa ammissibile", "base": 8000, "percent": 20, "source_refs": ["call:B"]},
            ]
        }
    )
    assert out["status"] == "scenario_result"
    assert out["jury_required"] is True
    assert len(out["scenarios"]) == 2
    assert [item["result"] for item in out["scenarios"]] == ["2000", "1600"]
    assert out["human_decision_required"] is True


def test_ambiguous_unit_requires_interpretation():
    out = CalculationOrchestrator().calculate("20% di 10k")
    assert out["status"] == "unit_ambiguous"
    assert out["jury_reason_codes"] == ["unit_interpretation"]


def test_missing_numeric_input_never_invokes_jury_to_invent():
    out = CalculationOrchestrator().calculate("20% di")
    assert out["status"] == "insufficient_input"
    assert out["jury_required"] is False


def test_explicit_simulation_marks_assumption():
    out = CalculationOrchestrator().calculate("simula 20% di 10000")
    assert out["status"] == "completed"
    assert out["assumptions"] == [{"assumed": True, "assumption_source": "user_requested_simulation"}]


def test_verified_result_cannot_be_overridden_by_jury_suggestion():
    out = CalculationOrchestrator().calculate(
        {
            "expression": "20% di 10000",
            "domain_context": {"official_percentage": 20, "jury_suggested_percentage": 30},
        }
    )
    assert out["answer"] == "2000"
    assert "jury_cannot_override_verified_result" in out["verification"]["checks"]


def test_explicit_cap_is_applied_deterministically():
    out = CalculationOrchestrator().calculate(
        {
            "expression": "50% di 10000",
            "domain_context": {"max_contribution": 3000},
        }
    )
    assert out["answer"] == "3000"
    assert out["jury_required"] is False


def test_official_percentage_not_changed_by_jury():
    out = CalculationOrchestrator().calculate(
        {
            "formula": "base * percent / 100",
            "values": {"base": 10000, "percent": 20},
            "domain_context": {"official_percentage": 20, "jury_suggested_percentage": 25},
        }
    )
    assert out["answer"] == "2000"
