from ralfloop_agent.domains.domain_approval_executor import build_approval_scope, execute_approved, request_domain_approval
from ralfloop_agent.domains.domain_approval_store import DomainApprovalStore
from ralfloop_agent.domains.domain_approval import DomainApprovalDecision

from domain_approval_fixtures import approval_env, canary_plan, make_domain


def test_scope_contains_hashes(approval_env):
    make_domain(approval_env / "domains")
    scope = build_approval_scope(action="run_domain_canary", bando_id="fondazione_unipolis_act_2026", version="1.0.0", canary_plan=canary_plan(), root=approval_env / "domains")
    assert scope["domain_manifest_hash"]
    assert scope["rule_set_hash"]
    assert scope["canary_plan_hash"]


def test_file_change_after_approval_marks_stale(approval_env):
    path = make_domain(approval_env / "domains")
    req = request_domain_approval(action="run_domain_canary", bando_id="fondazione_unipolis_act_2026", version="1.0.0", canary_plan=canary_plan(), root=approval_env / "domains")["request"]
    DomainApprovalStore().decide(DomainApprovalDecision(req["request_id"], "approve", 111, 111, 1, idempotency_key="x"), scope_digest_short=req["scope_digest_short"])
    (path / "rules" / "deadlines.yaml").write_text("changed: true\n", encoding="utf-8")
    out = execute_approved(req["request_id"], dry_run=True)
    assert out["status"] == "stale"
    assert "rule_set_changed" in out["stale_reasons"]
