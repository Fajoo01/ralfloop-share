from ralfloop_agent.domains.domain_approval import DomainApprovalDecision
from ralfloop_agent.domains.domain_approval_executor import request_domain_approval
from ralfloop_agent.domains.domain_approval_store import DomainApprovalStore

from domain_approval_fixtures import approval_env, canary_plan, make_domain


def test_default_off(monkeypatch, tmp_path):
    monkeypatch.delenv("RALFLOOP_ENABLE_TELEGRAM_APPROVAL_GATE", raising=False)
    make_domain(tmp_path / "domains")
    out = request_domain_approval(action="run_domain_canary", bando_id="fondazione_unipolis_act_2026", version="1.0.0", canary_plan=canary_plan(), root=tmp_path / "domains")
    assert out["status"] == "telegram_approval_gate_disabled"


def test_request_creation_and_valid_approval(approval_env):
    make_domain(approval_env / "domains")
    out = request_domain_approval(action="run_domain_canary", bando_id="fondazione_unipolis_act_2026", version="1.0.0", canary_plan=canary_plan(), root=approval_env / "domains")
    req = out["request"]
    assert req["request_id"].startswith("apr_")
    assert req["scope_digest_short"]
    store = DomainApprovalStore()
    result = store.decide(
        DomainApprovalDecision(req["request_id"], "approve", 111, 111, 7, idempotency_key="m1", chat_type="private"),
        scope_digest_short=req["scope_digest_short"],
    )
    assert result["status"] == "approved"


def test_valid_reject(approval_env):
    make_domain(approval_env / "domains")
    req = request_domain_approval(action="run_domain_canary", bando_id="fondazione_unipolis_act_2026", version="1.0.0", canary_plan=canary_plan(), root=approval_env / "domains")["request"]
    out = DomainApprovalStore().decide(DomainApprovalDecision(req["request_id"], "reject", 111, 111, 8, decision_reason="manca FAQ", idempotency_key="r1"), scope_digest_short=req["scope_digest_short"])
    assert out["status"] == "rejected"
