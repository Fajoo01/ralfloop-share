from __future__ import annotations

import hashlib
import json
from pathlib import Path

from ralfloop_agent.domains.recursive_mas_domain_instruct_profile import (
    EXPECTED,
    LINK_DIMENSIONS,
    audit_model_compatibility,
    evaluate_direct_text_gate,
    load_instruct_profile,
)
from ralfloop_agent.domains.recursive_mas_profiles import DOMAIN_PROFILE, MATH_PROFILE


MATH_PROFILE_SOURCE_SHA256 = "4c89714dfabe1603c0f73d48c452d761e9d460f533c6e23f658dfdcbe72921cf"


def _snapshot(root: Path, hidden_size: int) -> Path:
    root.mkdir()
    (root / "config.json").write_text(
        json.dumps(
            {
                "architectures": ["Qwen2ForCausalLM"],
                "hidden_size": hidden_size,
                "num_hidden_layers": 2,
                "vocab_size": 151936,
                "torch_dtype": "bfloat16",
            }
        )
    )
    (root / "tokenizer_config.json").write_text(json.dumps({"tokenizer_class": "Qwen2Tokenizer", "chat_template": "same-template"}))
    for name in ("merges.txt", "tokenizer.json", "vocab.json"):
        (root / name).write_text(f"same:{name}")
    return root


def test_math_profile_source_is_byte_for_byte_unchanged():
    source = Path(__file__).parents[1] / "ralfloop_agent/domains/recursive_mas_profiles.py"
    assert hashlib.sha256(source.read_bytes()).hexdigest() == MATH_PROFILE_SOURCE_SHA256
    assert MATH_PROFILE.profile_id == "recursive_mas_math"


def test_instruct_profile_is_separate_disabled_and_has_no_reused_adapters():
    profile = load_instruct_profile()
    assert profile["profile_id"] not in {MATH_PROFILE.profile_id, DOMAIN_PROFILE.profile_id}
    assert profile["enabled"] is False
    assert profile["inner_adapter_checkpoints"] == {}
    assert profile["cross_model_adapter_checkpoints"] == {}
    assert all("Instruct" in profile[f"{role}_model"] for role in ("planner", "critic", "solver"))


def test_real_model_config_and_hidden_size_compatibility_without_loading_weights(tmp_path):
    snapshots = {
        role: _snapshot(tmp_path / role, int(EXPECTED[role]["hidden_size"]))
        for role in ("planner", "critic", "solver")
    }
    result = audit_model_compatibility(snapshots)
    assert result["ok"] is True
    assert result["cross_model_dimensions"] == {
        "outer_12": [2048, 1536],
        "outer_23": [1536, 2048],
        "outer_31": [2048, 2048],
    }


def test_model_config_hidden_mismatch_is_rejected(tmp_path):
    snapshots = {
        role: _snapshot(tmp_path / role, 999 if role == "critic" else int(EXPECTED[role]["hidden_size"]))
        for role in ("planner", "critic", "solver")
    }
    result = audit_model_compatibility(snapshots)
    assert result["ok"] is False
    assert "hidden_size:critic" in result["errors"]


def test_tokenizer_and_chat_template_mismatch_is_rejected(tmp_path):
    snapshots = {
        role: _snapshot(tmp_path / role, int(EXPECTED[role]["hidden_size"]))
        for role in ("planner", "critic", "solver")
    }
    (snapshots["critic"] / "tokenizer_config.json").write_text(json.dumps({"tokenizer_class": "Qwen2Tokenizer", "chat_template": "different"}))
    result = audit_model_compatibility(snapshots)
    assert result["ok"] is False
    assert "tokenizer_hash_mismatch" in result["errors"]
    assert "chat_template_mismatch" in result["errors"]


def test_direct_text_planner_critic_and_solver_gate_thresholds():
    passed = evaluate_direct_text_gate(planner_valid=6, critic_valid=6, solver_valid=7)
    failed = evaluate_direct_text_gate(planner_valid=7, critic_valid=8, solver_valid=4)
    assert passed["passed"] is True
    assert failed["planner"]["passed"] is True
    assert failed["critic"]["passed"] is True
    assert failed["solver"]["passed"] is False
    assert failed["classification"] == "instruct_base_insufficient"


def test_direct_text_gate_rejects_invented_rule_or_source_ids():
    result = evaluate_direct_text_gate(planner_valid=8, critic_valid=8, solver_valid=8, invented_rule_ids=1)
    assert result["passed"] is False
    assert result["provenance"]["passed"] is False


def test_historical_math_shapes_are_not_mislabelled_as_new_instruct_shapes():
    assert LINK_DIMENSIONS == {"outer_12": (2048, 1536), "outer_23": (1536, 2048), "outer_31": (2048, 2048)}
