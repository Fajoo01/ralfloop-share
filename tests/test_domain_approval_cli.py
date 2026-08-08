import json
import sys
from types import ModuleType

if "ralfloop_agent.domains.bando_domain_builder" not in sys.modules:
    module = ModuleType("ralfloop_agent.domains.bando_domain_builder")
    module.BandoBuildRequest = object
    module.BandoDomainBuilder = object
    sys.modules["ralfloop_agent.domains.bando_domain_builder"] = module
if "ralfloop_agent.domains.bando_source_review" not in sys.modules:
    module = ModuleType("ralfloop_agent.domains.bando_source_review")
    module.assess_jury_sample = lambda **_kwargs: {"status": "stubbed"}
    module.build_jury_sample = lambda *_args, **_kwargs: []
    module.build_source_review = lambda *_args, **_kwargs: object()
    sys.modules["ralfloop_agent.domains.bando_source_review"] = module
if "ralfloop_agent.domains.bando_web_research" not in sys.modules:
    module = ModuleType("ralfloop_agent.domains.bando_web_research")
    module.BandoWebResearcher = object
    module.WebResearchRequest = object
    sys.modules["ralfloop_agent.domains.bando_web_research"] = module

from ralfloop_agent.domains import cli
from ralfloop_agent.domains.domain_approval import DomainApprovalDecision, now_ts
from ralfloop_agent.domains.domain_approval_store import DomainApprovalStore

from domain_approval_fixtures import approval_env, canary_plan, make_domain


def test_cli_request_status_and_dry_run(monkeypatch, capsys, approval_env):
    make_domain(approval_env / "domains")
    plan = approval_env / "canary.json"
    plan.write_text(json.dumps(canary_plan()), encoding="utf-8")
    monkeypatch.chdir(approval_env)
    rc = cli.main(["bandi", "request-approval", "--action", "run_domain_canary", "--bando", "fondazione_unipolis_act_2026", "--version", "1.0.0", "--canary-plan", str(plan), "--send-telegram"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    req_id = out["request"]["request_id"]
    assert out["telegram_delivery"]["status"] == "queued"
    rc = cli.main(["bandi", "approval-status", "--request-id", req_id])
    assert rc == 0
    assert json.loads(capsys.readouterr().out)["status"] == "pending"


def test_cli_expired_approved_request_blocks_dry_run(capsys, approval_env):
    make_domain(approval_env / "domains")
    plan = approval_env / "canary.json"
    plan.write_text(json.dumps(canary_plan()), encoding="utf-8")
    rc = cli.main(["bandi", "request-approval", "--action", "run_domain_canary", "--bando", "fondazione_unipolis_act_2026", "--version", "1.0.0", "--canary-plan", str(plan)])
    assert rc == 0
    req = json.loads(capsys.readouterr().out)["request"]
    DomainApprovalStore().decide(DomainApprovalDecision(req["request_id"], "approve", 111, 111, 77, idempotency_key="cli-expired"), scope_digest_short=req["scope_digest_short"])
    with DomainApprovalStore().connect() as conn:
        conn.execute("update approval_requests set expires_at = ? where request_id = ?", (now_ts() - 1, req["request_id"]))

    rc = cli.main(["bandi", "approval-status", "--request-id", req["request_id"]])
    assert rc == 0
    status = json.loads(capsys.readouterr().out)
    assert status["status"] == "expired"
    assert status["stored_status"] == "approved"
    assert status["effective_status"] == "expired"
    assert status["expired"] is True

    rc = cli.main(["bandi", "execute-approved", "--request-id", req["request_id"], "--dry-run"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "expired"
    assert out["effective_status"] == "expired"
    assert out["would_execute"] is False
    assert out["consumed"] is False
