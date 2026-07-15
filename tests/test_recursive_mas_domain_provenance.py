from __future__ import annotations

import hashlib
import inspect
import json
from pathlib import Path

import pytest

from ralfloop_agent.domains.recursive_mas_domain_provenance import (
    PROTOCOL_VERSION,
    build_evidence_packet,
    build_provenance_solver_prompt,
    detect_demo_contamination,
    parse_provenance_canonical_record,
    parse_provenance_solver_json,
    serialize_with_evidence_packet,
    structured_provenance_gate,
)
from ralfloop_agent.domains.recursive_mas_domain_serialization import (
    DEMO_RULE_ID,
    DEMO_SOURCE_ID,
    DomainSerializationError,
)
from ralfloop_agent.domains.recursive_mas_domain_training import _require_safe_runtime


def _packet():
    return build_evidence_packet(
        allowed_rule_ids=["R1", "R2", "R3"],
        allowed_source_ids=["S1", "S2"],
        planner_output={"rule_ids": ["R1", "R1", "R2"], "source_ids": ["S1", "S1"]},
        critic_output={
            "rule_assessments": [
                {"rule_id": "R1", "assessment": "valid"},
                {"rule_id": "R2", "assessment": "challenged by exception"},
            ],
            "source_assessments": [{"source_id": "S1", "assessment": "confirmed"}],
            "contradictions": [{"description": "R2 conflicts with R1"}],
        },
    )


def _raw(*, slots=True):
    support = {"text": "Support", "evidence_slots": [1, 1, 3]} if slots else "Support"
    counter = {"text": "Counter", "evidence_slots": [2]} if slots else "Counter"
    return json.dumps(
        {
            "position": "Conditional position",
            "support": [support],
            "counter": [counter],
            "uncertainty": ["Evidence incomplete"],
            "alternatives": ["Wait"],
            "recommendation": "Proceed after review",
            "confidence": 0.72,
            "human": True,
        }
    )


def test_evidence_packet_schema_is_exact_and_deduplicated():
    packet = _packet()
    assert packet.to_dict() == {
        "protocol_version": PROTOCOL_VERSION,
        "allowed_rule_ids": ["R1", "R2", "R3"],
        "allowed_source_ids": ["S1", "S2"],
        "planner_rule_ids": ["R1", "R2"],
        "planner_source_ids": ["S1"],
        "critic_confirmed_rule_ids": ["R1"],
        "critic_challenged_rule_ids": ["R2"],
        "critic_confirmed_source_ids": ["S1"],
        "critic_challenged_source_ids": [],
        "contradictions": [{"description": "R2 conflicts with R1"}],
    }


@pytest.mark.parametrize(
    "planner,critic,error",
    [
        ({"rule_ids": ["R9"]}, {}, "planner_rule_id_not_allowed:R9"),
        ({}, {"confirmed_source_ids": ["S9"]}, "critic_confirmed_source_id_not_allowed:S9"),
    ],
)
def test_packet_rejects_ids_outside_domain(planner, critic, error):
    with pytest.raises(DomainSerializationError, match=error):
        build_evidence_packet(
            allowed_rule_ids=["R1"],
            allowed_source_ids=["S1"],
            planner_output=planner,
            critic_output=critic,
        )


def test_planner_selection_and_critic_status_are_preserved():
    packet = _packet()
    assert packet.planner_rule_ids == ("R1", "R2")
    assert packet.planner_source_ids == ("S1",)
    assert packet.critic_confirmed_rule_ids == ("R1",)
    assert packet.critic_challenged_rule_ids == ("R2",)
    assert [slot.to_dict() for slot in packet.slots()] == [
        {"number": 1, "kind": "rule", "evidence_id": "R1", "selection_origin": "planner", "critic_status": "confirmed"},
        {"number": 2, "kind": "rule", "evidence_id": "R2", "selection_origin": "planner", "critic_status": "challenged"},
        {"number": 3, "kind": "source", "evidence_id": "S1", "selection_origin": "planner", "critic_status": "confirmed"},
    ]


def test_slot_mapping_is_validated_and_duplicate_slots_are_deduplicated():
    record = parse_provenance_solver_json(_raw(), _packet())
    assert record.support[0].evidence_slots == (1, 3)
    result = serialize_with_evidence_packet(record, {"domain_id": "d", "question": "q"}, _packet())
    assert result["payload"]["supporting_arguments"][0]["refs"] == ["R1", "S1"]


def test_slot_out_of_range_is_rejected():
    raw = json.loads(_raw())
    raw["support"][0]["evidence_slots"] = [4]
    with pytest.raises(DomainSerializationError, match="evidence_slot_out_of_range:4"):
        parse_provenance_solver_json(json.dumps(raw), _packet())


def test_missing_argument_provenance_is_marked_without_association_fabrication():
    record = parse_provenance_solver_json(_raw(slots=False), _packet())
    result = serialize_with_evidence_packet(record, {"domain_id": "d", "question": "q"}, _packet())
    assert result["metadata"]["argument_level_provenance_missing"] is True
    assert result["payload"]["supporting_arguments"][0]["refs"] == []
    assert result["payload"]["counterarguments"][0]["refs"] == []


def test_rule_application_uses_only_planner_rules_and_critic_status():
    result = serialize_with_evidence_packet(
        parse_provenance_solver_json(_raw(), _packet()), {"domain_id": "d", "question": "q"}, _packet()
    )
    assert result["payload"]["rule_application"] == [
        {
            "rule_id": "R1",
            "status": "confirmed",
            "application": "selected_by_planner_and_reviewed_by_critic",
        },
        {
            "rule_id": "R2",
            "status": "challenged",
            "application": "selected_by_planner_and_challenged_by_critic",
        },
    ]
    assert result["payload"]["evidence_used"] == [{"kind": "source", "id": "S1"}]


@pytest.mark.parametrize("demo_id", [DEMO_RULE_ID, DEMO_SOURCE_ID])
def test_demo_ids_are_rejected_and_detected(demo_id):
    raw = json.loads(_raw())
    raw["position"] += " " + demo_id
    assert detect_demo_contamination(json.dumps(raw)) is True
    with pytest.raises(DomainSerializationError, match="demo_id_not_allowed"):
        parse_provenance_solver_json(json.dumps(raw), _packet())


def test_final_serializer_changes_representation_not_semantics():
    record = parse_provenance_solver_json(_raw(), _packet())
    result = serialize_with_evidence_packet(record, {"domain_id": "d", "question": "q"}, _packet())
    payload = result["payload"]
    assert result["validation"]["ok"] is True
    assert payload["position"] == record.position
    assert payload["recommendation"] == record.recommendation
    assert [item["text"] for item in payload["supporting_arguments"]] == [item.text for item in record.support]
    assert [item["text"] for item in payload["counterarguments"]] == [item.text for item in record.counter]
    assert [item["evidence_id"] for item in result["metadata"]["evidence_confirmed"]] == ["R1", "S1"]
    assert [item["evidence_id"] for item in result["metadata"]["evidence_challenged"]] == ["R2"]


def test_solver_prompt_has_no_semantic_demo_and_never_requests_free_ids():
    case = {
        "question": "q",
        "facts": [{"fact_id": "F1", "statement": "fact"}],
        "rules": [{"rule_id": "R1", "statement": "rule"}, {"rule_id": "R2", "statement": "rule2"}],
        "sources": [{"source_id": "S1", "statement": "source"}],
    }
    for mode in ("none", "skeleton", "invalid_ids"):
        prompt = build_provenance_solver_prompt(case, {}, _packet(), request_slots=True, demo_mode=mode)
        assert '"rules"' not in prompt and '"sources"' not in prompt
        assert "synthetic painting" not in prompt and "painting_date" not in prompt
    assert DEMO_RULE_ID in build_provenance_solver_prompt(case, {}, _packet(), request_slots=True, demo_mode="invalid_ids")
    assert DEMO_SOURCE_ID in build_provenance_solver_prompt(case, {}, _packet(), request_slots=True, demo_mode="invalid_ids")


def test_no_slot_prompt_keeps_provenance_upstream():
    case = {"question": "q", "facts": [], "rules": [], "sources": []}
    prompt = build_provenance_solver_prompt(case, {}, _packet(), request_slots=False)
    assert "Do not output provenance IDs or evidence slots" in prompt
    assert "SUPPORT: <text>" in prompt
    assert "END mandatory" in prompt


def test_no_slot_canonical_record_preserves_semantics_without_ids():
    raw = """POSITION: Conditional position
SUPPORT: Supported by evidence
COUNTER: Strong objection
UNCERTAINTY: Evidence incomplete
ALTERNATIVE: Wait
RECOMMENDATION: Proceed after review
CONFIDENCE: 0.72
HUMAN: true
END"""
    record = parse_provenance_canonical_record(raw, _packet())
    assert record.support[0].text == "Supported by evidence"
    assert record.counter[0].evidence_slots == ()
    result = serialize_with_evidence_packet(record, {"domain_id": "d", "question": "q"}, _packet())
    assert result["validation"]["ok"] is True
    assert result["metadata"]["argument_level_provenance_missing"] is True


def test_no_slot_canonical_record_requires_end_and_rejects_unknown_line():
    with pytest.raises(DomainSerializationError, match="provenance_canonical_end_missing"):
        parse_provenance_canonical_record("POSITION: x", _packet())
    with pytest.raises(DomainSerializationError, match="provenance_canonical_unknown_line"):
        parse_provenance_canonical_record("POSITION: x\nFREE: y\nEND", _packet())


def test_no_slot_canonical_record_accepts_only_bounded_bullets_after_known_label():
    raw = """POSITION: Conditional position
SUPPORT:
- First support
- Second support
COUNTER:
- Strong objection
UNCERTAINTY:
- Evidence incomplete
ALTERNATIVE:
- Wait
RECOMMENDATION: Proceed after review
CONFIDENCE: 0.72
HUMAN: true
END"""
    record = parse_provenance_canonical_record(raw, _packet())
    assert [item.text for item in record.support] == ["First support", "Second support"]
    with pytest.raises(DomainSerializationError, match="provenance_canonical_unknown_line"):
        parse_provenance_canonical_record("POSITION: x\n- orphan\nEND", _packet())


def test_no_slot_canonical_record_accepts_single_bullet_for_known_scalar_label():
    raw = """POSITION: Conditional position
SUPPORT: Evidence
COUNTER: Objection
RECOMMENDATION:
- Proceed after review
CONFIDENCE: 0.72
HUMAN: true
END"""
    record = parse_provenance_canonical_record(raw, _packet())
    assert record.recommendation == "Proceed after review"


def test_structured_provenance_gate_checks_output_not_loss():
    metrics = {
        "semantic_complete_count": 7,
        "final_schema_valid_count": 8,
        "final_rule_accuracy": 0.875,
        "final_source_accuracy": 0.875,
        "invented_id_count": 0,
        "demo_contamination_count": 0,
        "contradiction_inclusion": 1.0,
        "counterargument_coverage": 1.0,
        "safety_violations": 0,
    }
    assert structured_provenance_gate(metrics) == {
        "passed": True,
        "classification": "instruct_base_sufficient_with_structured_provenance",
    }
    assert structured_provenance_gate({**metrics, "final_rule_accuracy": 0.874})["passed"] is False


def test_feature_math_and_telegram_invariants():
    root = Path(__file__).parents[1]
    math_profile = root / "ralfloop_agent/domains/recursive_mas_profiles.py"
    telegram_gate = root / "ralfloop_agent/domains/domain_approval_executor.py"
    assert hashlib.sha256(math_profile.read_bytes()).hexdigest() == "4c89714dfabe1603c0f73d48c452d761e9d460f533c6e23f658dfdcbe72921cf"
    assert hashlib.sha256(telegram_gate.read_bytes()).hexdigest() == "90031d96888449ea30baa45016f0980cf9e334f701ec037e00a46dff559db416"
    assert 'os.getenv("RALF_RECURSIVE_DOMAIN_REASONING", "0") != "0"' in inspect.getsource(_require_safe_runtime)
