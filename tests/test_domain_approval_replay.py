from ralfloop_agent.domains.domain_approval import DomainApprovalDecision
from ralfloop_agent.domains.domain_approval_executor import request_domain_approval
from ralfloop_agent.domains.domain_approval_store import DomainApprovalStore

from domain_approval_fixtures import approval_env, canary_plan, make_domain


def test_idempotency_and_double_approve(approval_env):
    make_domain(approval_env / "domains")
    req = request_domain_approval(action="run_domain_canary", bando_id="fondazione_unipolis_act_2026", version="1.0.0", canary_plan=canary_plan(), root=approval_env / "domains")["request"]
    store = DomainApprovalStore()
    decision = DomainApprovalDecision(req["request_id"], "approve", 111, 111, 1, idempotency_key="same")
    assert store.decide(decision, scope_digest_short=req["scope_digest_short"])["status"] == "approved"
    assert store.decide(decision, scope_digest_short=req["scope_digest_short"])["status"] == "approved"
    assert store.decide(DomainApprovalDecision(req["request_id"], "approve", 111, 111, 2, idempotency_key="second"), scope_digest_short=req["scope_digest_short"])["status"] == "already_approved"


def test_digest_mismatch(approval_env):
    make_domain(approval_env / "domains")
    req = request_domain_approval(action="run_domain_canary", bando_id="fondazione_unipolis_act_2026", version="1.0.0", canary_plan=canary_plan(), root=approval_env / "domains")["request"]
    out = DomainApprovalStore().decide(DomainApprovalDecision(req["request_id"], "approve", 111, 111, 1, idempotency_key="bad"), scope_digest_short="BAD-0000")
    assert out["status"] == "scope_digest_mismatch"
