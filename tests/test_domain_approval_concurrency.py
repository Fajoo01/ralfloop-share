import threading

from ralfloop_agent.domains.domain_approval import DomainApprovalDecision
from ralfloop_agent.domains.domain_approval_executor import execute_approved, request_domain_approval
from ralfloop_agent.domains.domain_approval_store import DomainApprovalStore

from domain_approval_fixtures import approval_env, canary_plan, make_domain


def test_two_decisions_leave_single_final_state(approval_env):
    make_domain(approval_env / "domains")
    req = request_domain_approval(action="run_domain_canary", bando_id="fondazione_unipolis_act_2026", version="1.0.0", canary_plan=canary_plan(), root=approval_env / "domains")["request"]
    results = []

    def decide(kind, key):
        out = DomainApprovalStore().decide(DomainApprovalDecision(req["request_id"], kind, 111, 111, len(results) + 1, idempotency_key=key), scope_digest_short=req["scope_digest_short"])
        results.append(out["status"])

    threads = [threading.Thread(target=decide, args=("approve", "a")), threading.Thread(target=decide, args=("reject", "b"))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    final = DomainApprovalStore().get_request(req["request_id"])["status"]
    assert final in {"approved", "rejected"}
    assert len([item for item in results if item in {"approved", "rejected"}]) == 1


def test_concurrent_execute_at_expiry_is_blocked(approval_env):
    make_domain(approval_env / "domains")
    req = request_domain_approval(action="run_domain_canary", bando_id="fondazione_unipolis_act_2026", version="1.0.0", canary_plan=canary_plan(), root=approval_env / "domains")["request"]
    DomainApprovalStore().decide(DomainApprovalDecision(req["request_id"], "approve", 111, 111, 1, idempotency_key="approved"), scope_digest_short=req["scope_digest_short"])
    store = DomainApprovalStore()
    with store.connect() as conn:
        conn.execute("update approval_requests set expires_at = strftime('%s','now') where request_id = ?", (req["request_id"],))
    results = []

    def run():
        results.append(execute_approved(req["request_id"], dry_run=True))

    threads = [threading.Thread(target=run), threading.Thread(target=run)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert {item["status"] for item in results} == {"expired"}
    assert all(item["would_execute"] is False for item in results)
    assert DomainApprovalStore().get_request(req["request_id"])["consumed_at"] is None
