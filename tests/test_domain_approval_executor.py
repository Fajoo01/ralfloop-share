from ralfloop_agent.domains.domain_approval import DomainApprovalDecision
from ralfloop_agent.domains.domain_approval_executor import execute_approved, request_domain_approval
from ralfloop_agent.domains.domain_approval_store import DomainApprovalStore

from domain_approval_fixtures import approval_env, canary_plan, make_domain


def test_dry_run_does_not_consume_and_real_execution_consumes(approval_env):
    make_domain(approval_env / "domains")
    req = request_domain_approval(action="run_domain_canary", bando_id="fondazione_unipolis_act_2026", version="1.0.0", canary_plan=canary_plan(), root=approval_env / "domains")["request"]
    DomainApprovalStore().decide(DomainApprovalDecision(req["request_id"], "approve", 111, 111, 1, idempotency_key="exec"), scope_digest_short=req["scope_digest_short"])
    dry = execute_approved(req["request_id"], dry_run=True)
    assert dry["status"] == "dry_run"
    assert DomainApprovalStore().get_request(req["request_id"])["status"] == "approved"
    real = execute_approved(req["request_id"], dry_run=False)
    assert real["consume_status"] == "consumed"
    assert execute_approved(req["request_id"], dry_run=False)["status"] == "already_consumed"


def test_promote_executes_on_temp_filesystem(approval_env):
    make_domain(approval_env / "domains")
    req = request_domain_approval(action="promote_domain", bando_id="fondazione_unipolis_act_2026", version="1.0.0", root=approval_env / "domains")["request"]
    DomainApprovalStore().decide(DomainApprovalDecision(req["request_id"], "approve", 111, 111, 2, idempotency_key="promote"), scope_digest_short=req["scope_digest_short"])
    out = execute_approved(req["request_id"])
    assert out["status"] == "executed"
    assert (approval_env / "domains" / "active" / "bandi" / "fondazione_unipolis_act_2026" / "1.0.0").exists()
