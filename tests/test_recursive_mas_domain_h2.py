from __future__ import annotations

import inspect
import json
from pathlib import Path
import sys

import pytest
import torch


REPO = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(REPO), str(REPO / "tools")]

import run_recursive_mas_domain_h2 as runner  # noqa: E402
from ralfloop_agent.domains.recursive_mas_domain_h2 import (  # noqa: E402
    ALLOWED_CAPS,
    H1B_SHA256,
    H2Config,
    OrderedOuter23,
    architecture_manifest,
    h2_gate,
    initialize_outer23,
    lab_manifest,
    layout_audit,
    micro_gate,
    ordered_cap,
    reserve_gate,
    response_only_labels,
    selection_key,
)


def metrics(**updates):
    value = {
        "semantic_complete_count": 21,
        "schema_valid_count": 21,
        "contradiction_recall": 0.66,
        "counterargument_coverage": 0.58,
        "recommendation_condition_correctness": 0.30,
        "rule_recall": 0.31,
        "rule_precision": 0.21,
        "source_recall": 0.37,
        "source_precision": 0.26,
        "invented_rule_ids": 0,
        "invented_source_ids": 0,
        "safety_violations": 0,
        "approval_violations": 0,
        "solver_format_regressions": 0,
        "format_errors": 0,
        "semantic_errors": 0,
        "adapter_transfer_errors": 0,
        "wall_median_ms": 1.0,
    }
    value.update(updates)
    return value


def test_h1b_sha_is_pinned() -> None:
    assert H1B_SHA256 == "7d48ad286a7b51138390e8845e2d41ddbf19dc6ae1d9daaae40f2cf579d42ad8"


def test_new_outer23_namespace_and_fp32() -> None:
    module = initialize_outer23(16)
    manifest = architecture_manifest(module)
    assert isinstance(module, OrderedOuter23)
    assert manifest["adapter_type"] == "outer_ln_res_adapter"
    assert manifest["dimensions"] == [1536, 2048]
    assert manifest["dtype"] == ["torch.float32"]


@pytest.mark.parametrize("cap", ALLOWED_CAPS)
def test_cap_keeps_first_n_positions_in_order(cap: int) -> None:
    value = torch.arange(40 * 1536, dtype=torch.float32).reshape(1, 40, 1536)
    selected = ordered_cap(value, cap)
    assert torch.equal(selected, value[:, :cap])


def test_only_cap16_cap32_allowed() -> None:
    value = torch.zeros((1, 40, 1536))
    with pytest.raises(ValueError, match="h2_cap_invalid"):
        ordered_cap(value, 24)
    assert H2Config().caps == (16, 32)


def test_no_pooling_contract() -> None:
    manifest = lab_manifest()
    source = inspect.getsource(runner._ce_step)
    assert manifest["pooling"] is None
    assert ".mean(" not in source
    assert "ordered_cap" in source


def test_solver_inner_bypassed() -> None:
    source = inspect.getsource(runner._ce_step) + inspect.getsource(runner._generate)
    assert "solver_inner(" not in source
    assert lab_manifest()["solver_inner_bypassed"] is True


def test_outer31_absent() -> None:
    assert lab_manifest()["outer31_present"] is False
    assert "outer31" not in OrderedOuter23.state_dict.__qualname__


def test_evidence_packet_text_marker_required() -> None:
    assert runner.PROMPT_MARKER == "IMMUTABLE EVIDENCE PACKET:"
    assert "record[\"prompt\"]" in inspect.getsource(runner._ce_step)


def test_latent_slot_layout_and_generation_boundary() -> None:
    audit = layout_audit(prefix_tokens=10, latent_tokens=16, suffix_tokens=20, response_tokens=8)
    assert audit["inputs_embeds_length"] == audit["attention_mask_length"] == 54
    assert audit["generation_boundary"] == 46
    assert audit["position_ids_monotonic"]


def test_response_only_ce_mask() -> None:
    response = torch.tensor([[7, 8, 9]])
    labels = response_only_labels(30, 16, response)
    assert labels.shape == (1, 49)
    assert torch.all(labels[:, :46] == -100)
    assert labels[:, 46:].tolist() == [[7, 8, 9]]


def test_only_outer23_trainable_claimed() -> None:
    manifest = lab_manifest()
    source = inspect.getsource(runner._train)
    assert manifest["trainable_components"] == ["new_outer23"]
    assert "optimizer = torch.optim.AdamW(outer.parameters()" in source


def test_base_model_frozen_loader() -> None:
    source = inspect.getsource(runner._model)
    assert "parameter.requires_grad = False" in source


def test_validation_selection_excludes_loss() -> None:
    source = inspect.getsource(selection_key)
    assert "loss" not in source
    assert selection_key(metrics(semantic_complete_count=22)) > selection_key(metrics())


def test_micro_gate_requires_six_and_gradient() -> None:
    passed = micro_gate(
        {"semantic_complete_count": 6, "outer23_gradient_norm": 1.0, "base_trainable_parameters": 0}
    )
    failed = micro_gate(
        {"semantic_complete_count": 5, "outer23_gradient_norm": 1.0, "base_trainable_parameters": 0}
    )
    assert passed["passed"] and not failed["passed"]


def test_h2_gate_valid_count_plus_three() -> None:
    baseline = metrics()
    candidate = metrics(semantic_complete_count=24, schema_valid_count=24)
    gate = h2_gate(candidate, baseline)
    assert gate["passed"] and gate["condition_1_valid_count"]


def test_h2_gate_relative_qualitative_ten_percent() -> None:
    baseline = metrics()
    candidate = metrics(
        semantic_complete_count=21,
        schema_valid_count=21,
        contradiction_recall=0.75,
        counterargument_coverage=0.65,
        recommendation_condition_correctness=0.35,
    )
    gate = h2_gate(candidate, baseline)
    assert gate["passed"] and gate["condition_2_qualitative_relative"]


def test_h2_gate_rejects_provenance_regression() -> None:
    baseline = metrics()
    candidate = metrics(semantic_complete_count=24, schema_valid_count=24, rule_recall=0.30)
    assert not h2_gate(candidate, baseline)["passed"]


def test_double_failure_closes_branch_without_reserve() -> None:
    source = inspect.getsource(runner.reserve_b)
    assert 'if not final_summary.get("h2_passed")' in source
    assert '"execution_count": 0' in source
    assert '"recursive_domain_branch_closed"' in source


def test_reserve_execution_guard_max_one() -> None:
    source = inspect.getsource(runner.reserve_b)
    assert "h2_reserve_b_execution_count_exceeded" in source
    assert '"execution_count": 1' in source


def test_reserve_gate_plus_two() -> None:
    baseline = metrics(semantic_complete_count=12)
    candidate = metrics(semantic_complete_count=14, schema_valid_count=21)
    assert reserve_gate(candidate, baseline)["passed"]


def test_freeze_required_before_final_a_cache() -> None:
    source = inspect.getsource(runner._cache_h1b_split)
    assert "h2_final_a_cache_before_freeze" in source


def test_final_a_runs_only_frozen_checkpoint() -> None:
    source = inspect.getsource(runner.final_a)
    assert "h2_final_a_already_executed" in source
    assert "_load_outer(frozen[\"checkpoint\"])" in source


def test_feature_flag_remains_disabled() -> None:
    profile = json.loads((REPO / "ralfloop_agent/domains/recursive_mas_domain_tokenwise_real_v1.json").read_text())
    assert profile["enabled"] is False
    assert "RALF_RECURSIVE_DOMAIN_REASONING=0" in inspect.getsource(runner.reserve_b)


def test_math_profile_not_targeted() -> None:
    source = Path(runner.__file__).read_text()
    assert "recursive_mas_math" not in source
    assert "math_profile_sha256" in source


def test_telegram_gate_not_targeted() -> None:
    source = Path(runner.__file__).read_text()
    assert "telegram_gate_sha256" in source
    assert "telegram" not in inspect.getsource(runner._train).lower()


def test_no_approval_or_service_mutation() -> None:
    source = Path(runner.__file__).read_text()
    forbidden = ("systemctl", "service restart", "create_approval", "execute_approval", "sudo")
    assert not any(value in source for value in forbidden)


def test_config_rejects_extra_variants() -> None:
    with pytest.raises(ValueError, match="h2_caps_invalid"):
        H2Config(caps=(8, 16)).validate()
