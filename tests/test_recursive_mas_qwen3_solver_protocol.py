from __future__ import annotations

import hashlib
import inspect
import json
from pathlib import Path

import pytest

from ralfloop_agent.domains.recursive_mas_domain_provenance import (
    build_evidence_packet,
    build_provenance_solver_prompt,
    lex_normalize_canonical_markers,
    parse_provenance_canonical_record,
    parse_provenance_canonical_record_bounded,
    same_line_canonical_markers,
)
from ralfloop_agent.domains.recursive_mas_domain_serialization import DomainSerializationError
from ralfloop_agent.domains.recursive_mas_domain_training import _require_safe_runtime
from ralfloop_agent.domains.recursive_mas_qwen3_solver_eval import (
    canonical_protocol_gate,
    classify_pipeline_error,
)


def _packet():
    return build_evidence_packet(
        allowed_rule_ids=["R1"],
        allowed_source_ids=["S1"],
        planner_output={"rule_ids": ["R1"], "source_ids": ["S1"]},
        critic_output={"confirmed_rule_ids": ["R1"], "confirmed_source_ids": ["S1"]},
    )


def _tail():
    return """UNCERTAINTY: Evidence incomplete
ALTERNATIVE: Wait
RECOMMENDATION: Proceed after review
CONFIDENCE: 0.75
HUMAN: true
END"""


def test_two_markers_on_one_line_are_bounded_and_declared():
    raw = "POSITION: Conditional position SUPPORT: Evidence\nCOUNTER: Objection\n" + _tail()
    result = parse_provenance_canonical_record_bounded(raw, _packet())
    assert result.strict_parse_valid is False
    assert result.normalized_parse_valid is True
    assert result.normalization_applied is True
    assert result.normalization_operations == ("insert_newline_before_SUPPORT",)
    assert result.content_unchanged is True
    assert result.record.position == "Conditional position"
    assert result.record.support[0].text == "Evidence"
    assert same_line_canonical_markers(raw) == ("SUPPORT",)


def test_three_markers_on_one_line_are_split_in_order():
    raw = "POSITION: Conditional SUPPORT: Evidence COUNTER: Objection\n" + _tail()
    result = parse_provenance_canonical_record_bounded(raw, _packet())
    assert result.normalized_parse_valid is True
    assert result.normalization_operations == (
        "insert_newline_before_SUPPORT",
        "insert_newline_before_COUNTER",
    )


def test_marker_inside_quoted_text_is_not_split():
    raw = 'POSITION: "La SUPPORT: teorica resta citata"\nSUPPORT: Evidence\nCOUNTER: Objection\n' + _tail()
    normalized = lex_normalize_canonical_markers(raw)
    assert normalized.operations == ()
    assert '"La SUPPORT: teorica resta citata"' in normalized.text
    assert parse_provenance_canonical_record(normalized.text, _packet()).position.startswith('"La SUPPORT:')


def test_lowercase_marker_is_not_recognized():
    raw = "POSITION: Conditional support: remains prose\nSUPPORT: Evidence\nCOUNTER: Objection\n" + _tail()
    normalized = lex_normalize_canonical_markers(raw)
    assert normalized.operations == ()
    assert "support: remains prose" in normalized.text


def test_unknown_uppercase_marker_is_rejected():
    raw = "POSITION: Conditional FOO: invented\nSUPPORT: Evidence\nCOUNTER: Objection\n" + _tail()
    with pytest.raises(DomainSerializationError, match="provenance_canonical_unknown_marker:FOO"):
        lex_normalize_canonical_markers(raw)


def test_missing_end_stays_invalid_and_is_never_invented():
    raw = "POSITION: Conditional SUPPORT: Evidence\nCOUNTER: Objection\n" + _tail().removesuffix("\nEND")
    result = parse_provenance_canonical_record_bounded(raw, _packet())
    assert result.normalization_applied is True
    assert result.normalized_parse_valid is False
    assert result.normalized_error == "provenance_canonical_end_missing"
    assert not result.normalization_operations or "insert_newline_before_END" not in result.normalization_operations


def test_end_on_same_line_is_split_only_when_already_present():
    raw = "POSITION: Conditional\nSUPPORT: Evidence\nCOUNTER: Objection\n" + _tail().replace("HUMAN: true\nEND", "HUMAN: true END")
    result = parse_provenance_canonical_record_bounded(raw, _packet())
    assert result.normalized_parse_valid is True
    assert result.normalization_operations == ("insert_newline_before_END",)


def test_marker_order_remains_strict():
    raw = "POSITION: Conditional\nCOUNTER: Objection\nSUPPORT: Evidence\n" + _tail()
    result = parse_provenance_canonical_record_bounded(raw, _packet())
    assert result.strict_parse_valid is False
    assert result.normalized_parse_valid is False
    assert result.strict_error == "provenance_canonical_order_invalid"


def test_normalizer_changes_only_whitespace_delimiters_and_invents_no_fields():
    raw = "POSITION: Conditional SUPPORT: Evidence\nCOUNTER: Objection\nEND"
    normalized = lex_normalize_canonical_markers(raw)
    compact = lambda value: "".join(value.split())
    assert compact(raw) == compact(normalized.text)
    assert normalized.content_unchanged is True
    result = parse_provenance_canonical_record_bounded(raw, _packet())
    assert result.normalized_parse_valid is False
    assert "UNCERTAINTY:" not in normalized.text
    assert "RECOMMENDATION:" not in normalized.text


def test_strengthened_prompt_contains_only_empty_skeleton():
    case = {"question": "q", "facts": [], "rules": [], "sources": []}
    prompt = build_provenance_solver_prompt(
        case, {}, _packet(), request_slots=False, demo_mode="none", strict_newlines=True
    )
    assert "Every marker MUST start on a new line" in prompt
    assert "Never put two markers on the same line" in prompt
    assert "POSITION:\nSUPPORT:\nCOUNTER:" in prompt
    assert "POSITION: <text>" not in prompt
    assert "RULE_DEMO_NEVER_VALID" not in prompt
    assert "SOURCE_DEMO_NEVER_VALID" not in prompt


def test_upstream_error_precedes_solver_format_error():
    assert classify_pipeline_error(
        packet_complete=False,
        solver_valid=False,
        serialization_valid=False,
        solver_error_class="solver_format_error",
    ) == "upstream_provenance_error"
    assert classify_pipeline_error(
        packet_complete=True,
        solver_valid=False,
        serialization_valid=False,
        solver_error_class="solver_format_error",
    ) == "solver_format_error"


def test_protocol_gate_accepts_seven_valid_plus_one_upstream_error():
    metrics = {
        "semantic_complete_count": 7,
        "final_schema_valid_count": 7,
        "upstream_provenance_errors": 1,
        "valid_packet_contradiction_inclusion": 1.0,
        "valid_packet_counterargument_coverage": 1.0,
        "valid_packet_uncertainty_presence": 1.0,
        "recommendation_presence": 1.0,
        "final_rule_accuracy": 0.875,
        "final_source_accuracy": 0.875,
        "foreign_ids_accepted": 0,
        "normalizer_content_inventions": 0,
        "demo_contamination_count": 0,
        "safety_violations": 0,
    }
    assert canonical_protocol_gate(metrics) == {
        "passed": True,
        "classification": "qwen3_solver_sufficient_for_micro_overfit",
    }
    assert canonical_protocol_gate({**metrics, "semantic_complete_count": 6})["passed"] is False


def test_feature_math_and_telegram_invariants():
    root = Path(__file__).parents[1]
    math_profile = root / "ralfloop_agent/domains/recursive_mas_profiles.py"
    telegram_gate = root / "ralfloop_agent/domains/domain_approval_executor.py"
    assert hashlib.sha256(math_profile.read_bytes()).hexdigest() == "4c89714dfabe1603c0f73d48c452d761e9d460f533c6e23f658dfdcbe72921cf"
    assert hashlib.sha256(telegram_gate.read_bytes()).hexdigest() == "90031d96888449ea30baa45016f0980cf9e334f701ec037e00a46dff559db416"
    assert 'os.getenv("RALF_RECURSIVE_DOMAIN_REASONING", "0") != "0"' in inspect.getsource(_require_safe_runtime)


def test_experimental_profile_is_disabled_and_not_runtime_registered():
    root = Path(__file__).parents[1]
    profile = json.loads(
        (root / "ralfloop_agent/domains/recursive_mas_domain_qwen3_solver_v1.json").read_text()
    )
    assert profile == {
        "profile_id": "recursive_mas_domain_qwen3_solver_v1",
        "enabled": False,
        "planner_model": "Qwen2.5-3B-Instruct",
        "critic_model": "Qwen2.5-1.5B-Instruct",
        "solver_model": "Qwen/Qwen3-1.7B",
        "solver_revision": "70d244cc86ccca08cf5af4e1e306ecf908b1ad5e",
        "thinking": False,
        "input_contract": "domain_evidence_packet_v1",
        "output_contract": "domain_opinion_v1",
    }
    assert "recursive_mas_domain_qwen3_solver_v1" not in (
        root / "ralfloop_agent/domains/recursive_mas_profiles.py"
    ).read_text()
