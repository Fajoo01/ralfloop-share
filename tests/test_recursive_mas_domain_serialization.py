from __future__ import annotations

import hashlib
import inspect
import json
from pathlib import Path

import pytest

from ralfloop_agent.domains.domain_opinion import DomainReasoningInput, validate_domain_opinion
from ralfloop_agent.domains.recursive_mas_domain_serialization import (
    DEMO_RULE_ID,
    DEMO_SOURCE_ID,
    DomainSerializationError,
    SerializationContext,
    SolverSemanticRecord,
    build_solver_semantic_prompt,
    detect_truncation,
    parse_canonical_record,
    parse_compact_json,
    semantic_complete,
    serialize_domain_opinion,
)
from ralfloop_agent.domains.recursive_mas_domain_training import _require_safe_runtime


COMPACT = {
    "position": "Conditional position",
    "support": ["Supported by supplied evidence"],
    "counter": ["Strongest supplied objection"],
    "rules": ["R1", "R1"],
    "sources": ["S1", "S1"],
    "uncertainty": ["Evidence remains incomplete"],
    "alternatives": ["Wait for verification"],
    "recommendation": "Proceed only after human verification",
    "confidence": 0.72,
    "human": True,
}
CANONICAL = """POSITION: Conditional position
SUPPORT: Supported by supplied evidence
COUNTER: Strongest supplied objection
RULES: R1,R1
SOURCES: S1,S1
UNCERTAINTY: Evidence remains incomplete
ALTERNATIVE: Wait for verification
RECOMMENDATION: Proceed only after human verification
CONFIDENCE: 0.72
HUMAN: true
END"""


def _context():
    return SerializationContext("synthetic", "Q?", ("R1", "R2"), ("S1", "S2"))


def test_compact_json_parser_enforces_bounded_contract():
    record = parse_compact_json(json.dumps(COMPACT))
    assert record.position == COMPACT["position"]
    assert record.support == tuple(COMPACT["support"])
    assert record.confidence == 0.72


def test_canonical_record_parser_enforces_fixed_protocol():
    record = parse_canonical_record(CANONICAL)
    assert record.rules == ("R1", "R1")
    assert record.sources == ("S1", "S1")
    assert record.human is True


def test_canonical_end_is_mandatory():
    with pytest.raises(DomainSerializationError, match="canonical_end_missing"):
        parse_canonical_record(CANONICAL.removesuffix("\nEND"))


def test_canonical_unknown_line_is_rejected():
    value = CANONICAL.replace("CONFIDENCE: 0.72", "EXPLANATION: hidden\nCONFIDENCE: 0.72")
    with pytest.raises(DomainSerializationError, match="canonical_unknown_line"):
        parse_canonical_record(value)


def test_canonical_out_of_order_line_is_rejected():
    value = CANONICAL.replace("RULES: R1,R1\nSOURCES: S1,S1", "SOURCES: S1,S1\nRULES: R1,R1")
    with pytest.raises(DomainSerializationError, match="canonical_order_invalid"):
        parse_canonical_record(value)


def test_rule_id_not_allowed_is_rejected():
    record = parse_compact_json(json.dumps({**COMPACT, "rules": ["R_UNKNOWN"]}))
    with pytest.raises(DomainSerializationError, match="rule_id_not_allowed"):
        serialize_domain_opinion(record, _context())


def test_source_id_not_allowed_is_rejected():
    record = parse_compact_json(json.dumps({**COMPACT, "sources": ["S_UNKNOWN"]}))
    with pytest.raises(DomainSerializationError, match="source_id_not_allowed"):
        serialize_domain_opinion(record, _context())


@pytest.mark.parametrize("field,value", [("support", ["Uses R_UNKNOWN"]), ("counter", ["Questions S_UNKNOWN"])])
def test_foreign_id_in_semantic_text_is_rejected(field, value):
    compact = {**COMPACT, field: value}
    record = parse_compact_json(json.dumps(compact))
    with pytest.raises(DomainSerializationError, match="_id_not_allowed"):
        serialize_domain_opinion(record, _context())


def test_allowed_id_followed_by_punctuation_is_accepted():
    compact = {**COMPACT, "counter": ["Rule R1. Source S1, both remain contestable."]}
    record = parse_compact_json(json.dumps(compact))
    payload = serialize_domain_opinion(record, _context())
    assert payload["counterarguments"][0]["text"] == compact["counter"][0]


def test_confidence_outside_zero_one_is_rejected():
    with pytest.raises(DomainSerializationError, match="confidence_out_of_range"):
        parse_compact_json(json.dumps({**COMPACT, "confidence": 1.1}))
    with pytest.raises(DomainSerializationError, match="confidence_out_of_range"):
        parse_canonical_record(CANONICAL.replace("0.72", "-0.1"))


def test_missing_essential_semantics_is_not_fabricated():
    incomplete = dict(COMPACT)
    incomplete["counter"] = []
    with pytest.raises(DomainSerializationError, match="solver_semantics_incomplete"):
        parse_compact_json(json.dumps(incomplete))


def test_serializer_changes_representation_only_and_deduplicates_ids():
    record = parse_compact_json(json.dumps(COMPACT))
    payload = serialize_domain_opinion(record, _context())
    assert payload["position"] == record.position
    assert [item["text"] for item in payload["supporting_arguments"]] == list(record.support)
    assert [item["text"] for item in payload["counterarguments"]] == list(record.counter)
    assert payload["recommendation"] == record.recommendation
    assert payload["rule_application"] == [{"rule_id": "R1", "application": "selected_by_solver"}]
    assert payload["evidence_used"] == [{"kind": "source", "id": "S1"}]


def test_serialized_output_is_real_domain_opinion_v1():
    payload = serialize_domain_opinion(parse_canonical_record(CANONICAL), _context())
    request = DomainReasoningInput(
        domain_id="synthetic",
        domain_version="v1",
        question="Q?",
        facts=(),
        rules=({"rule_id": "R1"}, {"rule_id": "R2"}),
        sources=({"source_id": "S1"}, {"source_id": "S2"}),
        constraints=(),
        known_contradictions=(),
    )
    assert validate_domain_opinion(payload, request)["ok"] is True


def test_truncation_is_detected_without_repairing_raw_semantics():
    raw = '{"position":"x","support":["y"],"counter":['
    result = detect_truncation(raw, token_count=512, max_new_tokens=512, eos_seen=False)
    assert result == {"truncated": True, "field": "counter", "content_recoverable": True}
    with pytest.raises(DomainSerializationError, match="compact_json_invalid"):
        parse_compact_json(raw)


def test_semantic_gate_requires_uncertainty_only_when_context_requires_it():
    record = SolverSemanticRecord("p", ("s",), ("c",), (), (), (), (), "r", 0.5, False)
    assert semantic_complete(record, uncertainty_required=False) is True
    assert semantic_complete(record, uncertainty_required=True) is False


def test_zero_shot_prompt_is_short_ordered_and_does_not_repeat_schema():
    case = {
        "question": "Q?",
        "facts": [{"fact_id": "F1", "statement": "fact"}],
        "rules": [{"rule_id": "R1", "statement": "rule"}],
        "sources": [{"source_id": "S1", "statement": "source"}],
    }
    critic = {"contradictions": [], "strongest_counterargument": "counter", "unresolved_issues": ["u"]}
    prompt = build_solver_semantic_prompt(case, critic, contract="B", one_shot=False)
    assert prompt.index("QUESTION") < prompt.index("FACTS") < prompt.index("RULES") < prompt.index("SOURCES") < prompt.index("CRITIC")
    assert prompt.count('"position"') == 1
    assert "UNRELATED EXAMPLE" not in prompt
    assert "RULES and SOURCES lines are mandatory and nonempty" in prompt


def test_one_shot_example_is_synthetic_and_separate_from_case():
    case = {"question": "dataset question", "facts": [], "rules": [], "sources": []}
    prompt = build_solver_semantic_prompt(case, {}, contract="C", one_shot=True)
    assert "synthetic painting" not in prompt
    assert "painting_date" not in prompt
    assert DEMO_RULE_ID in prompt and DEMO_SOURCE_ID in prompt
    assert prompt.count("dataset question") == 1
    assert prompt.index("STRUCTURE-ONLY EXAMPLE") < prompt.index("CURRENT CASE") < prompt.index("dataset question")
    assert "validator-rejected" in prompt
    assert "mandatory and nonempty" in prompt


def test_demo_ids_are_always_rejected_by_serializer():
    for field, value in (("rules", [DEMO_RULE_ID]), ("sources", [DEMO_SOURCE_ID])):
        record = parse_compact_json(json.dumps({**COMPACT, field: value}))
        with pytest.raises(DomainSerializationError, match="demo_id_not_allowed"):
            serialize_domain_opinion(record, _context())


def test_feature_flag_math_profile_and_telegram_gate_remain_invariant():
    root = Path(__file__).parents[1]
    math_profile = root / "ralfloop_agent/domains/recursive_mas_profiles.py"
    telegram_gate = root / "ralfloop_agent/domains/domain_approval_executor.py"
    assert hashlib.sha256(math_profile.read_bytes()).hexdigest() == "4c89714dfabe1603c0f73d48c452d761e9d460f533c6e23f658dfdcbe72921cf"
    assert hashlib.sha256(telegram_gate.read_bytes()).hexdigest() == "90031d96888449ea30baa45016f0980cf9e334f701ec037e00a46dff559db416"
    assert 'os.getenv("RALF_RECURSIVE_DOMAIN_REASONING", "0") != "0"' in inspect.getsource(_require_safe_runtime)
