import json

from ralfloop_agent.domains.domain_approval import DomainApprovalDecision, approval_status_response, effective_approval_status, now_ts
from ralfloop_agent.domains.domain_approval_executor import execute_approved, request_domain_approval
from ralfloop_agent.domains.domain_approval_store import DomainApprovalStore

from domain_approval_fixtures import approval_env, canary_plan, make_domain


def _request(approval_env, *, action="run_domain_canary"):
    make_domain(approval_env / "domains")
    return request_domain_approval(
        action=action,
        bando_id="fondazione_unipolis_act_2026",
        version="1.0.0",
        canary_plan=canary_plan() if action == "run_domain_canary" else None,
        root=approval_env / "domains",
    )["request"]


def _approve(req, key="approve"):
    return DomainApprovalStore().decide(
        DomainApprovalDecision(req["request_id"], "approve", 111, 111, 1, idempotency_key=key),
        scope_digest_short=req["scope_digest_short"],
    )


def _set_request_fields(request_id, **fields):
    store = DomainApprovalStore()
    assignments = ", ".join(f"{key} = ?" for key in fields)
    with store.connect() as conn:
        conn.execute(f"update approval_requests set {assignments} where request_id = ?", (*fields.values(), request_id))


def _row(request_id):
    return DomainApprovalStore().get_request(request_id)


def test_expired_request_rejects_approval(monkeypatch, approval_env):
    monkeypatch.setenv("RALFLOOP_TELEGRAM_APPROVAL_TTL_SEC", "-1")
    make_domain(approval_env / "domains")
    req = request_domain_approval(action="run_domain_canary", bando_id="fondazione_unipolis_act_2026", version="1.0.0", canary_plan=canary_plan(), root=approval_env / "domains")["request"]
    out = DomainApprovalStore().decide(DomainApprovalDecision(req["request_id"], "approve", 111, 111, 1, idempotency_key="e"), scope_digest_short=req["scope_digest_short"])
    assert out["status"] == "expired"


def test_effective_status_handles_pending_and_approved_expiry(approval_env):
    req = _request(approval_env)
    future = now_ts() + 100
    past = now_ts() - 1
    _set_request_fields(req["request_id"], expires_at=future)
    assert effective_approval_status(_row(req["request_id"])) == "pending"
    _set_request_fields(req["request_id"], expires_at=past)
    assert effective_approval_status(_row(req["request_id"])) == "expired"
    _set_request_fields(req["request_id"], status="approved", expires_at=future)
    assert effective_approval_status(_row(req["request_id"])) == "approved"
    _set_request_fields(req["request_id"], expires_at=past)
    assert effective_approval_status(_row(req["request_id"])) == "expired"


def test_now_equal_expires_at_is_expired(approval_env):
    req = _request(approval_env)
    row = _row(req["request_id"])
    assert effective_approval_status(row, now=int(row["expires_at"])) == "expired"


def test_approval_status_reports_stored_and_effective_status(approval_env):
    req = _request(approval_env)
    _approve(req)
    _set_request_fields(req["request_id"], expires_at=now_ts() - 1)
    out = approval_status_response(_row(req["request_id"]))
    assert out["status"] == "expired"
    assert out["stored_status"] == "approved"
    assert out["effective_status"] == "expired"
    assert out["expired"] is True
    assert out["request"]["status"] == "approved"


def test_expired_approved_dry_run_does_not_execute_or_consume(monkeypatch, approval_env):
    req = _request(approval_env)
    _approve(req)
    _set_request_fields(req["request_id"], expires_at=now_ts() - 1)
    called = False

    def fail(_row):
        nonlocal called
        called = True
        raise AssertionError("canary executor must not be called")

    monkeypatch.setattr("ralfloop_agent.domains.domain_approval_executor._execute_run_domain_canary", fail)
    out = execute_approved(req["request_id"], dry_run=True)
    assert out["status"] == "expired"
    assert out["stored_status"] == "approved"
    assert out["effective_status"] == "expired"
    assert out["would_execute"] is False
    assert out["consumed"] is False
    assert called is False
    assert _row(req["request_id"])["consumed_at"] is None
    with DomainApprovalStore().connect() as conn:
        events = [json.loads(item[0]) for item in conn.execute("select event_json from approval_audit_events").fetchall()]
    blocked = [item for item in events if item["event"] == "execution_blocked_expired"]
    assert blocked
    event = blocked[-1]
    assert event["bando_id"] == "fondazione_unipolis_act_2026"
    assert event["version"] == "1.0.0"
    assert event["scope_digest"]
    assert event["action"] == "run_domain_canary"
    assert event["request_id"] == req["request_id"]
    assert event["old_status"] == "approved"
    assert event["new_status"] == "expired"
    assert event["result"] == "expired"
    rendered = json.dumps(event, sort_keys=True)
    assert "secret" not in rendered.lower()
    assert "nonce" not in rendered.lower()
    assert "telegram_message" not in rendered


def test_expired_approved_real_execution_does_not_call_canary(monkeypatch, approval_env):
    req = _request(approval_env)
    _approve(req)
    _set_request_fields(req["request_id"], expires_at=now_ts() - 1)
    called = False

    def fail(_row):
        nonlocal called
        called = True
        raise AssertionError("canary executor must not be called")

    monkeypatch.setattr("ralfloop_agent.domains.domain_approval_executor._execute_run_domain_canary", fail)
    out = execute_approved(req["request_id"], dry_run=False)
    assert out["status"] == "expired"
    assert out["would_execute"] is False
    assert called is False
    assert _row(req["request_id"])["consumed_at"] is None


def test_expired_approval_does_not_call_promoter_or_source_updater(monkeypatch, approval_env):
    promote = _request(approval_env, action="promote_domain")
    update = _request(approval_env, action="apply_domain_source_update")
    _approve(promote, "promote")
    _approve(update, "update")
    _set_request_fields(promote["request_id"], expires_at=now_ts() - 1)
    _set_request_fields(update["request_id"], expires_at=now_ts() - 1)
    calls = []
    monkeypatch.setattr("ralfloop_agent.domains.domain_approval_executor._execute_promote", lambda row: calls.append(("promote", row)) or {"status": "executed"})
    monkeypatch.setattr("ralfloop_agent.domains.domain_approval_executor._execute_apply_domain_source_update", lambda row: calls.append(("update", row)) or {"status": "source_update_authorized"})
    assert execute_approved(promote["request_id"])["status"] == "expired"
    assert execute_approved(update["request_id"])["status"] == "expired"
    assert calls == []


def test_expired_precedes_stale_and_consumed_precedes_expired(approval_env):
    req = _request(approval_env)
    _approve(req)
    _set_request_fields(req["request_id"], expires_at=now_ts() - 1)
    domain_file = approval_env / "domains" / "drafts" / "bandi" / "fondazione_unipolis_act_2026" / "1.0.0" / "rules" / "deadlines.yaml"
    domain_file.write_text(domain_file.read_text(encoding="utf-8") + "\n# changed\n", encoding="utf-8")
    assert execute_approved(req["request_id"], dry_run=True)["status"] == "expired"

    live = _request(approval_env)
    _approve(live, "live")
    assert execute_approved(live["request_id"])["consume_status"] == "consumed"
    _set_request_fields(live["request_id"], expires_at=now_ts() - 1)
    assert execute_approved(live["request_id"])["status"] == "already_consumed"
