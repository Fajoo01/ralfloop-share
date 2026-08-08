from ralfloop_agent.domains.domain_approval_executor import execute_approved, request_domain_approval

from domain_approval_fixtures import approval_env, make_domain


def test_promotion_without_approval_is_blocked(approval_env):
    make_domain(approval_env / "domains")
    assert execute_approved("apr_missing")["status"] == "not_found"


def test_no_active_created_at_request_time(approval_env):
    make_domain(approval_env / "domains")
    out = request_domain_approval(action="promote_domain", bando_id="fondazione_unipolis_act_2026", version="1.0.0", root=approval_env / "domains")
    assert out["status"] == "pending"
    assert not (approval_env / "domains" / "active" / "bandi" / "fondazione_unipolis_act_2026" / "1.0.0").exists()
