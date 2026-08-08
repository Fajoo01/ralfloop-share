from __future__ import annotations

import hashlib
import inspect
from pathlib import Path

from ralfloop_agent.domains.recursive_mas_domain_provenance import (
    build_evidence_packet,
    build_provenance_solver_prompt,
)
from ralfloop_agent.domains.recursive_mas_domain_training import _require_safe_runtime
from ralfloop_agent.domains.recursive_mas_qwen3_solver_eval import (
    QWEN3_SOLVER_HIDDEN_SIZE,
    QWEN3_SOLVER_MODEL_ID,
    QWEN3_SOLVER_REVISION,
    aggregate_solver_results,
    build_gold_packet,
    build_real_packet,
    classify_pipeline_error,
    detect_qwen3_template_modes,
    end_to_end_gate,
    evaluate_solver_final,
    final_only_from_thinking,
    fixture_hash_record,
    gold_solver_gate,
    stable_sha256,
    validate_qwen3_metadata,
)


def _case():
    return {
        "id": "c1",
        "domain_id": "d",
        "domain_version": "v1",
        "question": "q",
        "facts": [],
        "rules": [{"rule_id": "R1", "statement": "rule"}],
        "sources": [{"source_id": "S1", "statement": "source"}],
        "known_contradictions": [],
        "gold": {"required_rules": ["R1"], "required_sources": ["S1"]},
    }


def _trace():
    return {
        "case_id": "c1",
        "planner_target": {"rules_selected": ["R1"], "sources_selected": ["S1"]},
        "critic_target": {
            "contradictions": [],
            "rule_application_errors": [],
            "source_provenance_errors": [],
        },
        "domain_opinion_target": {},
    }


def _packet():
    return build_evidence_packet(
        allowed_rule_ids=["R1"],
        allowed_source_ids=["S1"],
        planner_output={"rule_ids": ["R1"], "source_ids": ["S1"]},
        critic_output={"confirmed_rule_ids": ["R1"], "confirmed_source_ids": ["S1"]},
    )


def _record():
    return """POSITION: Conditional position
SUPPORT: Evidence supports the position
COUNTER: Strong objection remains
UNCERTAINTY: Evidence is incomplete
ALTERNATIVE: Wait
RECOMMENDATION: Proceed after review
CONFIDENCE: 0.72
HUMAN: true
END"""


def _result(**changes):
    result = {
        "semantic_complete": True,
        "final_schema_valid": True,
        "contradiction_inclusion": True,
        "counterargument_coverage": True,
        "uncertainty_presence": True,
        "recommendation_presence": True,
        "foreign_ids_accepted": [],
        "foreign_ids_generated": [],
        "provenance_ids_emitted": [],
        "structured_provenance_regenerated": False,
        "demo_contamination": False,
        "error_class": None,
    }
    result.update(changes)
    return result


def test_qwen3_metadata_audit_hidden_size_and_revision_pin():
    audit = {
        "model_id": QWEN3_SOLVER_MODEL_ID,
        "requested_revision": QWEN3_SOLVER_REVISION,
        "effective_revision": QWEN3_SOLVER_REVISION,
        "model_type": "qwen3",
        "architecture": "Qwen3ForCausalLM",
        "hidden_size": QWEN3_SOLVER_HIDDEN_SIZE,
        "num_hidden_layers": 28,
        "vocab_size": 151936,
        "tokenizer_class": "Qwen2Tokenizer",
        "license": "apache-2.0",
        "revision_pinned": True,
        "missing_required_files": [],
    }
    assert validate_qwen3_metadata(audit) == {"ok": True, "errors": [], "hidden_embedding_compatible": True}
    assert validate_qwen3_metadata({**audit, "hidden_size": 1536})["hidden_embedding_compatible"] is False
    assert validate_qwen3_metadata({**audit, "effective_revision": "floating"})["ok"] is False


def test_chat_template_mode_detection_and_thinking_exclusion():
    template = "{% if enable_thinking is false %}<|im_start|>assistant{% else %}<think>x</think>{% endif %}"
    modes = detect_qwen3_template_modes(template)
    assert modes["thinking_supported"] is True
    assert modes["non_thinking_supported"] is True
    assert modes["non_thinking_argument"] == {"enable_thinking": False}
    assert final_only_from_thinking("<think>private</think>FINAL") == {
        "final": "FINAL",
        "reasoning_discarded": True,
        "thinking_closed": True,
    }
    assert final_only_from_thinking("<think>unfinished")["final"] == ""


def test_same_fixture_hashes_and_packet_builders_are_deterministic():
    gold = build_gold_packet(_case(), _trace())
    real = build_real_packet(
        _case(),
        {"rule_ids": ["R1"], "source_ids": ["S1"]},
        {"confirmed_rule_ids": ["R1"], "confirmed_source_ids": ["S1"]},
    )
    assert gold.to_dict() == real.to_dict()
    first = fixture_hash_record(_case(), _trace(), gold)
    second = fixture_hash_record(_case(), _trace(), real)
    assert first == second
    assert stable_sha256([first]) == stable_sha256([second])


def test_gold_and_real_packet_runner_does_not_regenerate_provenance():
    scored = evaluate_solver_final(final_text=_record(), case=_case(), trace=_trace(), packet=_packet())
    assert scored["semantic_complete"] is True
    assert scored["final_schema_valid"] is True
    assert scored["structured_provenance_regenerated"] is False
    assert scored["provenance_ids_emitted"] == []
    assert [item["rule_id"] for item in scored["payload"]["rule_application"]] == ["R1"]
    assert scored["payload"]["evidence_used"] == [{"kind": "source", "id": "S1"}]


def test_upstream_solver_and_serialization_errors_are_separate():
    assert classify_pipeline_error(packet_complete=False, solver_valid=True, serialization_valid=True) == "upstream_provenance_error"
    assert classify_pipeline_error(packet_complete=True, solver_valid=False, serialization_valid=False) == "solver_semantic_error"
    assert classify_pipeline_error(packet_complete=True, solver_valid=True, serialization_valid=False) == "serialization_error"
    assert classify_pipeline_error(packet_complete=True, solver_valid=True, serialization_valid=True) is None


def test_prompt_uses_no_semantic_demo_and_does_not_request_ids():
    prompt = build_provenance_solver_prompt(_case(), {}, _packet(), request_slots=False, demo_mode="none")
    assert "Do not output provenance IDs or evidence slots" in prompt
    assert "RULE_DEMO_NEVER_VALID" not in prompt
    assert "SOURCE_DEMO_NEVER_VALID" not in prompt
    assert "few-shot" not in prompt.casefold()


def test_gold_and_end_to_end_gates_measure_outputs():
    metrics = aggregate_solver_results([_result() for _ in range(8)])
    assert gold_solver_gate(metrics)["passed"] is True
    e2e = {**metrics, "final_rule_accuracy": 0.875, "final_source_accuracy": 0.875, "safety_violations": 0}
    assert end_to_end_gate(e2e) == {"passed": True, "classification": "qwen3_solver_sufficient_for_micro_overfit"}
    assert end_to_end_gate({**e2e, "final_schema_valid_count": 7})["passed"] is False


def test_feature_math_and_telegram_invariants():
    root = Path(__file__).parents[1]
    math_profile = root / "ralfloop_agent/domains/recursive_mas_profiles.py"
    telegram_gate = root / "ralfloop_agent/domains/domain_approval_executor.py"
    assert hashlib.sha256(math_profile.read_bytes()).hexdigest() == "4c89714dfabe1603c0f73d48c452d761e9d460f533c6e23f658dfdcbe72921cf"
    assert hashlib.sha256(telegram_gate.read_bytes()).hexdigest() == "90031d96888449ea30baa45016f0980cf9e334f701ec037e00a46dff559db416"
    assert 'os.getenv("RALF_RECURSIVE_DOMAIN_REASONING", "0") != "0"' in inspect.getsource(_require_safe_runtime)
