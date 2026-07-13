from ralfloop_agent.domains.domain_approval_executor import request_domain_approval
from ralfloop_agent.domains.domain_approval import DomainApprovalDecision
from ralfloop_agent.domains.domain_approval_store import DomainApprovalStore

from domain_approval_fixtures import approval_env, canary_plan, make_domain


def test_canary_policy_requires_rule_and_expected_answer(approval_env):
    make_domain(approval_env / "domains")
    out = request_domain_approval(action="run_domain_canary", bando_id="fondazione_unipolis_act_2026", version="1.0.0", canary_plan={}, root=approval_env / "domains")
    assert out["status"] == "canary_policy_denied"


def test_promotion_policy_requires_draft(approval_env):
    path = make_domain(approval_env / "domains")
    (path / "domain.yaml").write_text((path / "domain.yaml").read_text().replace("state: draft", "state: active"), encoding="utf-8")
    out = request_domain_approval(action="promote_domain", bando_id="fondazione_unipolis_act_2026", version="1.0.0", root=approval_env / "domains")
    assert out["status"] == "promotion_policy_denied"


def test_user_chat_and_group_policy(monkeypatch, approval_env):
    make_domain(approval_env / "domains")
    req = request_domain_approval(action="run_domain_canary", bando_id="fondazione_unipolis_act_2026", version="1.0.0", canary_plan=canary_plan(), root=approval_env / "domains")["request"]
    store = DomainApprovalStore()
    assert store.decide(DomainApprovalDecision(req["request_id"], "approve", 999, 111, 1, idempotency_key="u"), scope_digest_short=req["scope_digest_short"])["status"] == "unauthorized"
    assert store.decide(DomainApprovalDecision(req["request_id"], "approve", 111, 999, 2, idempotency_key="c"), scope_digest_short=req["scope_digest_short"])["status"] == "chat_unauthorized"
    assert store.decide(DomainApprovalDecision(req["request_id"], "approve", 111, 111, 3, idempotency_key="g", chat_type="group"), scope_digest_short=req["scope_digest_short"])["status"] == "private_chat_required"
