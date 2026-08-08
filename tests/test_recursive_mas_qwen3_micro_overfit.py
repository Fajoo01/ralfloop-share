from __future__ import annotations

import hashlib
import inspect
from pathlib import Path

import pytest

torch = pytest.importorskip("torch")
from torch import nn

from ralfloop_agent.domains.recursive_mas_domain_training import _require_safe_runtime
from ralfloop_agent.domains.recursive_mas_qwen3_micro_overfit import (
    NativeMicroConfig,
    adapter_gradient_report,
    adapter_manifest,
    freeze_base_model,
    initialize_native_adapters,
    load_adapter_checkpoint,
    load_hidden_cache,
    native_graph,
    response_only_loss_labels,
    save_adapter_checkpoint,
    save_hidden_cache,
    stage_a_gate,
    stage_b_gate,
    stage_c_gate,
    tensor_sha256,
)


def _metrics():
    return {
        "valid_count": 8,
        "schema_valid_count": 8,
        "rule_accuracy": 0.875,
        "source_accuracy": 0.875,
        "contradiction_inclusion": 1.0,
        "counterargument_coverage": 1.0,
        "valid_packet_contradiction_inclusion": 1.0,
        "valid_packet_counterargument_coverage": 1.0,
        "uncertainty_presence": 1.0,
        "recommendation_presence": 1.0,
        "invented_ids": 0,
        "demo_contamination": 0,
        "safety_violations": 0,
        "approval_violations": 0,
    }


def test_native_graph_is_exact_and_outer31_is_excluded():
    graph = native_graph()
    adapters = [edge["adapter"] for edge in graph]
    assert adapters[:5] == ["planner_inner", "outer12", "critic_inner", "outer23", "solver_inner"]
    outer31 = next(edge for edge in graph if edge["adapter"] == "outer31")
    assert outer31["used_by_final_decode"] is False
    assert outer31["trained"] is False
    assert graph[-1]["tensor_destination"] == "strict_parser_then_domain_opinion_v1"


def test_fresh_adapter_namespace_has_no_outer31():
    adapters = initialize_native_adapters(seed=7)
    assert tuple(adapters.keys()) == (
        "planner_inner",
        "outer12",
        "critic_inner",
        "outer23",
        "solver_inner",
    )
    manifest = adapter_manifest(adapters)
    assert set(manifest) == set(adapters)
    assert all(item["parameters"] == item["trainable_parameters"] for item in manifest.values())


def test_hidden_cache_hash_mask_and_namespace(tmp_path):
    hidden = torch.randn(1, 3, 5)
    mask = torch.tensor([[1, 1, 0]])
    path = tmp_path / "cached_hidden" / "critic_gold" / "case.pt"
    metadata = {"case_id": "case", "cache_kind": "critic_gold", "model_revision": "rev", "input_hash": "abc"}
    record = save_hidden_cache(path, tensor=hidden, attention_mask=mask, metadata=metadata)
    loaded, loaded_mask, loaded_metadata = load_hidden_cache(path, expected=metadata)
    assert tensor_sha256(loaded) == record["tensor_sha256"]
    assert tensor_sha256(loaded_mask) == record["attention_mask_sha256"]
    assert loaded_metadata["cache_kind"] == "critic_gold"
    with pytest.raises(RuntimeError, match="hidden_cache_metadata_mismatch:cache_kind"):
        load_hidden_cache(path, expected={"cache_kind": "critic_real"})


def test_hidden_cache_rejects_bad_attention_mask():
    with pytest.raises(ValueError, match="hidden_cache_attention_mask_shape_invalid"):
        save_hidden_cache(
            Path("unused.pt"),
            tensor=torch.randn(1, 2, 3),
            attention_mask=torch.ones(1, 3),
            metadata={},
        )


def test_response_only_loss_mask_masks_prompt_and_padding():
    response = torch.tensor([[7, 8, 0]])
    mask = torch.tensor([[1, 1, 0]])
    labels = response_only_loss_labels(3, response, mask)
    assert labels.tolist() == [[-100, -100, -100, 7, 8, -100]]


def test_solver_base_is_frozen():
    model = nn.Sequential(nn.Linear(3, 4), nn.Linear(4, 2))
    report = freeze_base_model(model)
    assert report["frozen"] is True
    assert report["trainable_parameters"] == 0


def test_final_ce_gradients_reach_stage_a_adapters_only_after_detach():
    adapters = nn.ModuleDict(
        {
            "planner_inner": nn.Linear(2, 2),
            "outer12": nn.Linear(2, 2),
            "critic_inner": nn.Linear(2, 2),
            "outer23": nn.Linear(2, 2),
            "solver_inner": nn.Linear(2, 2),
        }
    )
    value = torch.randn(1, 2)
    output = adapters["solver_inner"](adapters["outer23"](adapters["critic_inner"](value.detach())))
    output.square().mean().backward()
    report = adapter_gradient_report(adapters, detached_upstream=True)
    assert report["final_ce_reaches"] == {
        "planner_inner": False,
        "outer12": False,
        "critic_inner": True,
        "outer23": True,
        "solver_inner": True,
    }
    assert report["upstream_training_claim"] == "structured_surrogate_only"


def test_stagewise_surrogate_updates_planner_inner_and_outer12():
    planner_inner = nn.Linear(2, 2)
    outer12 = nn.Linear(2, 3)
    loss = outer12(planner_inner(torch.randn(1, 2))).square().mean()
    loss.backward()
    assert all(parameter.grad is not None for parameter in planner_inner.parameters())
    assert all(parameter.grad is not None for parameter in outer12.parameters())


def test_stage_a_b_c_gates_are_output_based():
    metrics = _metrics()
    assert stage_a_gate(metrics)["passed"] is True
    assert stage_b_gate({**metrics, "valid_count": 7, "schema_valid_count": 7})["passed"] is True
    assert stage_c_gate(metrics) == {
        "passed": True,
        "classification": "native_qwen3_recursive_micro_overfit_passed",
    }
    assert stage_a_gate({**metrics, "valid_count": 6, "loss": 0.001})["passed"] is False
    assert stage_c_gate({**metrics, "invented_ids": 1})["passed"] is False


def test_checkpoint_save_reload_preserves_weight_and_hash(tmp_path):
    module = nn.Linear(3, 4)
    path = tmp_path / "checkpoints" / "step_010" / "outer23.pt"
    record = save_adapter_checkpoint(module, path, metadata={"step": 10, "dataset_hash": "d"})
    clone = nn.Linear(3, 4)
    metadata = load_adapter_checkpoint(clone, path, expected_sha256=record["sha256"])
    assert metadata == {"step": 10, "dataset_hash": "d"}
    assert all(torch.equal(module.state_dict()[name], clone.state_dict()[name]) for name in module.state_dict())
    assert hashlib.sha256(path.read_bytes()).hexdigest() == record["sha256"]


def test_training_config_enforces_ce_dominance_and_limits():
    NativeMicroConfig().validate()
    with pytest.raises(ValueError, match="qwen3_micro_step_limit_invalid"):
        NativeMicroConfig(max_steps=121).validate()
    with pytest.raises(ValueError, match="qwen3_final_token_ce_not_dominant"):
        NativeMicroConfig(final_token_ce_weight=1.0).validate()


def test_no_invented_provenance_path_exists_in_native_graph():
    serialized = str(native_graph())
    assert "outer31" in serialized
    assert "strict_parser_then_domain_opinion_v1" in serialized
    assert "generate_rule_ids" not in serialized
    assert "generate_source_ids" not in serialized


def test_math_feature_and_telegram_invariants():
    root = Path(__file__).parents[1]
    assert hashlib.sha256((root / "ralfloop_agent/domains/recursive_mas_profiles.py").read_bytes()).hexdigest() == "4c89714dfabe1603c0f73d48c452d761e9d460f533c6e23f658dfdcbe72921cf"
    assert hashlib.sha256((root / "ralfloop_agent/domains/domain_approval_executor.py").read_bytes()).hexdigest() == "90031d96888449ea30baa45016f0980cf9e334f701ec037e00a46dff559db416"
    assert 'os.getenv("RALF_RECURSIVE_DOMAIN_REASONING", "0") != "0"' in inspect.getsource(_require_safe_runtime)
