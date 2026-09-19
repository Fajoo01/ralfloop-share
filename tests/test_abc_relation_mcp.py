from __future__ import annotations

from datetime import datetime, timezone

from ralfloop_agent.abc_relation.mcp import LOCAL_WRITE_TOOLS, RelationMCPServer
from ralfloop_agent.abc_relation.service import RelationService
from ralfloop_agent.abc_relation.store import RelationStore


def _server(tmp_path):
    service = RelationService(RelationStore(tmp_path / "abc.sqlite3"))
    return RelationMCPServer(service)


def _structured(response):
    assert response["isError"] is False
    assert response["structuredContent"]["ok"] is True
    return response["structuredContent"]["result"]


def test_tool_surface_has_no_outbound_or_surveillance_actions(tmp_path):
    server = _server(tmp_path)
    names = {row["name"] for row in server.list_tools()}

    assert LOCAL_WRITE_TOOLS <= names
    assert not any("send" in name or "track" in name or "message" in name for name in names)
    policy = _structured(server.call("abc_policy_status", {}))
    assert policy["outbound_actions"] is False
    assert policy["surveillance"] is False


def test_record_fact_then_analyze_with_provenance(tmp_path):
    server = _server(tmp_path)
    event = _structured(server.call("abc_record_event", {
        "occurred_at": datetime(2026, 9, 19, 18, 0, tzinfo=timezone.utc).isoformat(),
        "kind": "observed_fact",
        "summary": "A concrete low-pressure invitation was accepted.",
        "source_kind": "manual",
        "source_ref": "manual:test",
        "confidence": 0.9,
        "weight": 18,
        "tags": ["invitation"],
    }))["event"]

    assert event["event_id"].startswith("abc_evt_")
    assert event["provenance"]["source_ref"] == "manual:test"

    analysis = _structured(server.call("abc_analyze", {}))
    assert analysis["active_event_count"] == 1
    assert analysis["observed_fact_count"] == 1
    assert analysis["relcalc"]["score"] > 50


def test_mind_reading_stays_inference_and_triggers_warning(tmp_path):
    server = _server(tmp_path)
    _structured(server.call("abc_record_event", {
        "occurred_at": "2026-09-19T18:00:00+00:00",
        "kind": "inference",
        "summary": "Interpretation of tone without direct confirmation.",
        "source_kind": "manual",
        "source_ref": "manual:test",
        "confidence": 0.9,
        "weight": 40,
        "tags": ["mind_reading", "tone"],
    }))

    analysis = _structured(server.call("abc_analyze", {}))
    assert analysis["inference_count"] == 1
    assert "intrusive_or_mind_reading_evidence_present" in analysis["warnings"]
    assert "mind_reading_risk" in analysis["relcalc"]["bias_flags"]
    assert analysis["relcalc"]["confidence"] <= 0.5


def test_reference_library_contains_dialogue_and_empirical_frameworks(tmp_path):
    server = _server(tmp_path)
    value = _structured(server.call("abc_get_reference_library", {}))
    frameworks = {row["framework"] for row in value["references"]}

    assert "dialogo_strategico" in frameworks
    assert "motivational_interviewing" in frameworks
    assert "investment_model_interdependence" in frameworks
    strategic = next(row for row in value["references"] if row["framework"] == "dialogo_strategico")
    assert "intenzioni nascoste" in strategic["not_evidence_for"]


def test_create_snapshot_is_local_and_keeps_model_estimates_labeled(tmp_path):
    server = _server(tmp_path)
    value = _structured(server.call("abc_create_snapshot", {
        "label": "test state",
        "state_summary": "Observed continuity with unresolved ambiguity.",
        "hypotheses": [{
            "key": "continuity",
            "statement": "Continuity remains plausible.",
            "probability": 0.6,
            "confidence": 0.4,
            "status": "weak",
        }],
        "strategy_rules": [{
            "rule_id": "no_pressure",
            "text": "Prefer low-pressure, direct communication.",
            "priority": 90,
        }],
    }))

    assert value["side_effects"] == "local_db_only"
    assert value["snapshot"]["hypotheses"][0]["confidence"] == 0.4
    state = _structured(server.call("abc_get_state", {}))
    assert state["snapshot"]["label"] == "test state"


def test_naive_datetime_is_rejected(tmp_path):
    server = _server(tmp_path)
    response = server.call("abc_record_event", {
        "occurred_at": "2026-09-19T18:00:00",
        "kind": "observed_fact",
        "summary": "Fact",
        "source_kind": "manual",
        "source_ref": "manual:test",
    })
    assert response["isError"] is True
    assert response["structuredContent"]["error"] == "MALFORMED_REQUEST"


def test_local_writes_can_be_disabled(tmp_path):
    service = RelationService(RelationStore(tmp_path / "abc.sqlite3"))
    server = RelationMCPServer(service, allow_local_writes=False)
    names = {row["name"] for row in server.list_tools()}
    assert not (LOCAL_WRITE_TOOLS & names)
