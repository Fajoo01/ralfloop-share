import json
import time

from ralfloop_agent.domains.builder import DomainBuilder
from ralfloop_agent.domains.models import DomainManifest
from ralfloop_agent.domains.registry import DomainRegistry
from ralfloop_agent.domains.storage import now_iso, sha256_tree, write_text_atomic, write_yaml_atomic
from ralfloop_agent.integration.cheshire_domain_bridge import (
    CheshireDomainBridge,
    CheshireDomainBridgeConfig,
    CheshireDomainBridgeRequest,
)


def _bridge(tmp_path, *, enabled=True, jury=False, native=False, runtime=None, fail_open=True, timeout_sec=5, registry=None):
    return CheshireDomainBridge(
        CheshireDomainBridgeConfig(
            enabled=enabled,
            enable_domain_jury=jury,
            enable_recursive_mas_native=native,
            allow_legacy_fallback=True,
            timeout_sec=timeout_sec,
            fail_open=fail_open,
            audit_enabled=False,
        ),
        registry=registry or DomainRegistry(tmp_path / "domains"),
        runtime_controller=runtime,
    )


def test_feature_flag_off_returns_legacy_without_resolution(tmp_path, monkeypatch):
    bridge = _bridge(tmp_path, enabled=False)

    def boom(*args, **kwargs):
        raise AssertionError("resolver must not run")

    monkeypatch.setattr("ralfloop_agent.integration.cheshire_domain_bridge.DomainResolver.resolve", boom)
    result = bridge.execute({"message": "Quanto fa 2 + 3?", "domain": "arithmetic_basic"})
    assert result.route == "legacy"
    assert result.legacy_fallback_required is True
    assert result.fallback_reason == "domain_orchestrator_disabled"
    assert bridge.to_cheshire_response(result)["handled"] is False


def test_deterministic_answer_without_jury(tmp_path):
    result = _bridge(tmp_path).execute({"message": "Quanto fa 2 + 3?", "domain": "arithmetic_basic"})
    response = _bridge(tmp_path).to_cheshire_response(result)
    assert result.status == "completed"
    assert result.answer == "5"
    assert result.jury_required is False
    assert response["handled"] is True
    assert response["source"] == "domain_deterministic"
    assert response["jury_used"] is False


def test_qualitative_requires_jury_when_disabled(tmp_path):
    bridge = _bridge(tmp_path, enabled=True, jury=False, native=False)
    result = bridge.execute({"message": "Qual è la strategia più prudente per questo incidente?", "domain": "incident_triage"})
    response = bridge.to_cheshire_response(result)
    assert result.status == "jury_required"
    assert result.jury_status == "disabled"
    assert result.answer is None
    assert response["handled"] is True


def test_native_jury_enabled_invokes_controller_with_compact_bundle(tmp_path):
    class Runtime:
        calls = []

        def execute(self, request):
            self.calls.append(request)
            payload = json.loads(request["goal"])
            assert payload["domain_id"] == "incident_triage"
            assert payload["unresolved_questions"] == ["recommendation_required"]
            assert "deterministic_conclusions" in payload
            return {"ok": True, "status": "completed", "answer": "Risposta giuria", "native_latent_verified": True}

    runtime = Runtime()
    bridge = _bridge(tmp_path, jury=True, native=True, runtime=runtime)
    result = bridge.execute({"message": "Qual è la strategia più prudente per questo incidente?", "domain": "incident_triage"})
    response = bridge.to_cheshire_response(result)
    assert len(runtime.calls) == 1
    assert result.source == "domain_jury_recursive_mas"
    assert response["jury_used"] is True
    assert response["native_latent_verified"] is True


def test_domain_missing_blocks_invented_answer(tmp_path):
    bridge = _bridge(tmp_path)
    result = bridge.execute({"message": "Valuta il rischio di un dominio mai registrato"})
    response = bridge.to_cheshire_response(result)
    assert result.domain_creation_required is True
    assert response["handled"] is True
    assert response["domain_creation_required"] is True


def test_side_effect_requires_human_confirmation(tmp_path):
    bridge = _bridge(tmp_path)
    result = bridge.execute({"message": "Invia telegram per incidente", "domain": "incident_triage"})
    response = bridge.to_cheshire_response(result)
    assert result.human_confirmation_required is True
    assert response["handled"] is True
    assert response["human_confirmation_required"] is True


def test_draft_and_quarantined_domains_are_ignored(tmp_path):
    reg = DomainRegistry(tmp_path / "domains")
    source = tmp_path / "manual.md"
    source.write_text("source", encoding="utf-8")
    draft = DomainBuilder(reg).create_draft("Draft only", [str(source)], {"domain_id": "draft_only"})
    reg.quarantine("draft_only", draft.version)
    bridge = _bridge(tmp_path, registry=reg)
    result = bridge.execute({"message": "x", "domain": "draft_only"})
    assert result.domain_creation_required is True


def test_checksum_invalid_active_domain_falls_back(tmp_path):
    reg = DomainRegistry(tmp_path / "domains")
    path = reg.root / "active" / "bad_hash" / "1.0.0"
    _write_active_domain(path, "bad_hash")
    write_text_atomic(path / "manifest.sha256", "wrong\n")
    reg.promote("bad_hash", "1.0.0", path)
    result = _bridge(tmp_path, registry=reg).execute({"message": "bad hash", "domain": "bad_hash"})
    assert result.legacy_fallback_required is True
    assert result.fallback_reason == "domain_integrity_failed"


def test_timeout_fail_open_and_fail_closed(tmp_path):
    class SlowRuntime:
        def execute(self, request):
            time.sleep(0.2)
            return {"ok": True, "status": "completed", "answer": "late"}

    open_bridge = _bridge(tmp_path, jury=True, native=True, runtime=SlowRuntime(), timeout_sec=0)
    open_result = open_bridge.execute({"message": "Qual è la strategia più prudente?", "domain": "incident_triage"})
    assert open_result.status == "jury_timeout"
    assert open_bridge.to_cheshire_response(open_result)["handled"] is True

    closed_bridge = _bridge(tmp_path, enabled=True, fail_open=False)
    err = closed_bridge._error_or_legacy(CheshireDomainBridgeRequest.from_message("x"), time.monotonic(), "a", "ValueError", "boom")
    assert err.status == "error"
    assert err.legacy_fallback_required is False


def test_busy_and_circuit_open_do_not_claim_native_success(tmp_path):
    class Runtime:
        calls = 0

        def __init__(self, status):
            self.status = status

        def execute(self, request):
            self.calls += 1
            return {"ok": False, "status": self.status, "native_latent_verified": False}

    busy = Runtime("busy")
    bridge = _bridge(tmp_path, jury=True, native=True, runtime=busy)
    result = bridge.execute({"message": "Quale strategia prudente?", "domain": "incident_triage"})
    assert busy.calls == 1
    assert result.status == "jury_busy"
    assert result.native_latent_verified is False

    circuit = Runtime("circuit_open")
    result2 = _bridge(tmp_path, jury=True, native=True, runtime=circuit).execute({"message": "Quale strategia prudente?", "domain": "incident_triage"})
    assert result2.status == "circuit_open"
    assert result2.native_latent_verified is False


def test_handled_true_blocks_legacy_and_handled_false_leaves_legacy(tmp_path):
    bridge = _bridge(tmp_path)
    handled = bridge.to_cheshire_response(bridge.execute({"message": "Quanto fa 2 + 3?", "domain": "arithmetic_basic"}))
    legacy = bridge.to_cheshire_response(_bridge(tmp_path, enabled=False).execute({"message": "Quanto fa 2 + 3?", "domain": "arithmetic_basic"}))
    assert handled["handled"] is True
    assert "legacy_fallback_required" not in handled
    assert legacy["handled"] is False
    assert legacy["legacy_fallback_required"] is True


def _write_active_domain(path, domain_id):
    now = now_iso()
    manifest = DomainManifest(
        domain_id=domain_id,
        display_name=domain_id,
        description="test",
        version="1.0.0",
        state="active",
        created_at=now,
        updated_at=now,
        created_by="test",
        approved_by="human",
        approved_at=now,
        scope=[domain_id.replace("_", " ")],
        out_of_scope=["side effects"],
        source_requirements=["approved_local_policy"],
        deterministic_capabilities=[],
        jury_capabilities=[],
        external_action_policy="deny",
        rule_precedence=[],
        minimum_source_count=1,
        minimum_test_pass_rate=1.0,
        content_hash="",
        schema_version="1.0",
        jury_review_completed=True,
        red_team_completed=True,
    )
    write_yaml_atomic(path / "domain.yaml", manifest.to_dict())
    write_text_atomic(path / "sources.jsonl", "")
    write_text_atomic(path / "conflicts.jsonl", "")
    write_text_atomic(path / "manifest.sha256", sha256_tree(path) + "\n")
