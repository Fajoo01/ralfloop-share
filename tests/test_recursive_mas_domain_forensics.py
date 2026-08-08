from __future__ import annotations

import hashlib
import inspect
import json

import pytest

torch = pytest.importorskip("torch")

from ralfloop_agent.domains.domain_opinion import DomainReasoningInput, validate_domain_opinion
from ralfloop_agent.domains.recursive_mas_domain_diagnostics import (
    compare_hidden_states,
    diagnose_final_decode,
    load_verified_checkpoint,
    micro_overfit_gate,
    select_ablation_extremes,
)
from ralfloop_agent.domains.recursive_mas_domain_prompts import build_domain_solver_prompt_with_slots
from ralfloop_agent.domains.recursive_mas_domain_training import _require_safe_runtime, load_component, save_component
from ralfloop_agent.domains.recursive_mas_profiles import DOMAIN_PROFILE, MATH_ADAPTER_HASHES, MATH_PROFILE


def _request() -> DomainReasoningInput:
    return DomainReasoningInput(
        domain_id="synthetic",
        domain_version="v1",
        question="Q?",
        facts=({"fact_id": "f1"},),
        rules=({"rule_id": "r1"},),
        sources=({"source_id": "s1"},),
        constraints=(),
        known_contradictions=(),
    )


def _valid_opinion() -> dict:
    return {
        "domain_id": "synthetic",
        "question": "Q?",
        "position": "opinion",
        "supporting_arguments": [{"text": "x", "kind": "inference", "refs": ["f1"]}],
        "counterarguments": [],
        "rule_application": [{"rule_id": "r1", "inference": "x"}],
        "evidence_used": [{"kind": "source", "id": "s1"}],
        "uncertainties": [],
        "alternative_interpretations": [],
        "recommendation": "none",
        "confidence": 0.5,
        "human_decision_required": False,
    }


def test_checkpoint_hash_and_opened_path_are_verified(tmp_path):
    module = torch.nn.Linear(3, 3)
    path = tmp_path / "adapter.pt"
    torch.save(module.state_dict(), path)
    expected = hashlib.sha256(path.read_bytes()).hexdigest()
    opened = []

    def loader(value, **kwargs):
        opened.append(str(value.resolve()))
        return torch.load(value, **kwargs)

    restored = torch.nn.Linear(3, 3)
    record = load_verified_checkpoint(restored, path, expected, loader=loader)
    assert record["opened_path"] == opened[0]
    assert record["loaded_sha256"] == expected
    assert record["parameter_count"] == sum(item.numel() for item in restored.parameters())
    assert all(torch.equal(module.state_dict()[name], restored.state_dict()[name]) for name in module.state_dict())


def test_adapter_modifies_hidden_state_with_finite_statistics():
    adapter = torch.nn.Linear(4, 4, bias=False)
    with torch.no_grad():
        adapter.weight.copy_(2 * torch.eye(4))
    hidden = torch.ones(1, 2, 4)
    comparison = compare_hidden_states(hidden, adapter(hidden))
    assert comparison["classification"] == "adapter_applied"
    assert comparison["l2_delta"] > 0
    assert comparison["after"]["nan"] is False
    assert comparison["after"]["inf"] is False


def test_optimizer_contains_parameters_gradients_and_changes_weight():
    module = torch.nn.Linear(2, 2)
    optimizer = torch.optim.SGD(module.parameters(), lr=0.1)
    assert {id(item) for item in module.parameters()} == {id(item) for group in optimizer.param_groups for item in group["params"]}
    before = {name: value.detach().clone() for name, value in module.state_dict().items()}
    loss = module(torch.ones(1, 2)).square().mean()
    loss.backward()
    assert all(item.grad is not None and float(item.grad.norm()) > 0 for item in module.parameters())
    optimizer.step()
    assert any(not torch.equal(before[name], module.state_dict()[name]) for name in before)


def test_save_reload_preserves_final_weight_not_metadata_only(tmp_path):
    module = torch.nn.Linear(2, 2)
    path = tmp_path / "adapter.pt"
    digest = save_component(module, path, {"step": 1})
    restored = load_component(torch.nn.Linear(2, 2), path)
    assert len(digest) == 64
    assert all(torch.equal(module.state_dict()[name], restored.state_dict()[name]) for name in module.state_dict())
    assert restored.state_dict()


def test_raw_decode_is_captured_and_invalid_json_classified():
    result = diagnose_final_decode("not json", token_ids=[10, 11], eos_token_id=99, max_new_tokens=8, rendered_prompt="domain_opinion_v1")
    assert result["raw_captured"] is True
    assert len(result["raw_sha256"]) == 64
    assert result["primary"] == "invalid_json_only"


def test_early_eos_and_wrong_template_are_classified():
    early = diagnose_final_decode("x", token_ids=[99], eos_token_id=99, max_new_tokens=8, rendered_prompt="domain_opinion_v1")
    wrong = diagnose_final_decode("{}", token_ids=[1], eos_token_id=99, max_new_tokens=8, rendered_prompt="missing contract")
    assert early["primary"] == "early_eos"
    assert wrong["primary"] == "wrong_chat_template"


def test_final_decode_valid_json_passes_real_parser_and_validator():
    payload = _valid_opinion()
    validation = validate_domain_opinion(payload, _request())
    result = diagnose_final_decode(json.dumps(payload), token_ids=[1, 2, 3, 4, 99], eos_token_id=99, max_new_tokens=8, rendered_prompt="domain_opinion_v1", validation=validation)
    assert validation["ok"] is True
    assert result["primary"] == "valid"


def test_adapter_ablation_uses_semantics_then_cosine_regression():
    empty = {"schema_validity": 0, "rule_accuracy": 0, "source_accuracy": 0, "contradiction_recall": 0}
    result = select_ablation_extremes({
        "A_none": {"aggregate": empty, "mean_cosine_vs_baseline": 1.0},
        "G_all": {"aggregate": empty, "mean_cosine_vs_baseline": 0.01},
    })
    assert result == {"best": "A_none", "worst": "G_all"}


def test_micro_overfit_end_to_end_gate_requires_contract_and_provenance():
    failed = micro_overfit_gate({"valid_count": 0, "schema_validity": 0, "rule_accuracy": 0, "source_accuracy": 0})
    passed = micro_overfit_gate({"valid_count": 7, "schema_validity": 1, "rule_accuracy": 1, "source_accuracy": 1})
    assert failed["classification"] == "current_native_architecture_not_trainable_for_domain_contract"
    assert passed["success"] is True


def test_math_profile_and_feature_flag_remain_invariant(monkeypatch):
    assert MATH_PROFILE.profile_id == "recursive_mas_math"
    assert MATH_PROFILE.profile_version == "upstream-38f7da45"
    assert MATH_PROFILE.supported_reason_codes == ()
    assert MATH_ADAPTER_HASHES["planner"] == "025560b16fa170403e80163499b54481988a7f32590e3eb27e9b98b3e85ba0f9"
    assert DOMAIN_PROFILE.enabled is False
    monkeypatch.setenv("RALF_RECURSIVE_DOMAIN_REASONING", "0")

    class Cuda:
        @staticmethod
        def is_available():
            return True

        @staticmethod
        def mem_get_info():
            return 8_000_000_000, 8_000_000_000

    _require_safe_runtime(type("Torch", (), {"cuda": Cuda})())
    assert 'os.getenv("RALF_RECURSIVE_DOMAIN_REASONING", "0") != "0"' in inspect.getsource(_require_safe_runtime)


def test_approval_invariants_remain_explicit_and_validator_blocks_approval():
    prompt = build_domain_solver_prompt_with_slots("{}")
    assert "Do not authorize actions or approvals" in prompt
    payload = _valid_opinion()
    payload["recommendation"] = "auto-approve"
    validation = validate_domain_opinion(payload, _request())
    assert "forbidden_action_or_approval" in validation["errors"]
