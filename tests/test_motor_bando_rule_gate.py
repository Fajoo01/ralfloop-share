from decimal import Decimal

from ralfloop_agent.integration.motor_bando_rule_gate import (
    budget_balance_check,
    evaluate_structured_rules,
    extract_bando_structured_evidence,
    hard_rule_violation,
)


def _texts():
    app = """Forma giuridica*\nAssociazioni di promozione sociale\nDurata del progetto in mesi*\n24 mesi\nIl progetto ha dei partner?\nNo\n6. ALLEGATI\nBudget*\nQuadro logico*\nStatuto dell'organizzazione*\n"""
    budget = """TOTALE COSTI 100000,00\nContributo richiesto a Fondazione Demo* 25000,00\n% del contributo sul totale dei costi 25%\nTOTALE ENTRATE 50000\n"""
    return app, budget


def test_extracts_structured_bando_values():
    app, budget = _texts()
    evidence = extract_bando_structured_evidence(app, budget_text=budget)
    assert evidence.requested_contribution_eur == Decimal("25000.00")
    assert evidence.requested_contribution_rate == Decimal("0.25")
    assert evidence.total_costs_eur == Decimal("100000.00")
    assert evidence.total_income_eur == Decimal("50000")
    assert evidence.duration_months == 24
    assert evidence.has_partners is False


def test_numeric_rules_are_deterministic_and_fail_closed():
    app, budget = _texts()
    evidence = extract_bando_structured_evidence(app, budget_text=budget)
    rules = [
        {"rule_id": "min", "field": "contribution_min_amount", "value": 75000},
        {"rule_id": "max", "field": "contribution_max_amount", "value": 150000},
        {"rule_id": "rate", "field": "contribution_max_rate", "value": 0.75},
        {"rule_id": "duration", "field": "duration", "value": {"max_months": 24}},
    ]
    checks = evaluate_structured_rules(rules, evidence)
    assert [check.status for check in checks] == ["VIOLATED", "SATISFIED", "SATISFIED", "SATISFIED"]
    assert hard_rule_violation(checks)


def test_applicant_and_conditional_partnership_rules():
    app, budget = _texts()
    evidence = extract_bando_structured_evidence(app, budget_text=budget)
    rules = [
        {"rule_id": "entity", "field": "eligibility_applicant_types", "value": ["Associazioni di promozione sociale", "Cooperative sociali"]},
        {"rule_id": "agreement", "field": "partnership_agreement", "value": "required_if_partnership"},
    ]
    checks = evaluate_structured_rules(rules, evidence)
    assert [check.status for check in checks] == ["SATISFIED", "SATISFIED"]


def test_budget_gap_is_review_signal_not_hard_gate():
    app, budget = _texts()
    evidence = extract_bando_structured_evidence(app, budget_text=budget)
    check = budget_balance_check(evidence)
    assert check.status == "VIOLATED"
    assert check.reason == "budget_totals_mismatch"
    assert check.hard is False
