from ralfloop_agent.domains.domain_approval_executor import execute_approved, request_domain_approval

from domain_approval_fixtures import approval_env, make_domain


def test_canary_without_approval_is_blocked(approval_env):
    make_domain(approval_env / "domains")
    out = execute_approved("missing")
    assert out["status"] == "not_found"


def test_canary_with_jury_expected_is_denied(approval_env):
    make_domain(approval_env / "domains")
    plan = {"expected_rule_id": "application_deadline", "expected_deterministic_answer": "x", "jury_expected": True}
    out = request_domain_approval(action="run_domain_canary", bando_id="fondazione_unipolis_act_2026", version="1.0.0", canary_plan=plan, root=approval_env / "domains")
    assert out["status"] == "canary_policy_denied"
