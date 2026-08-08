from __future__ import annotations

import inspect
from pathlib import Path
import sys

import pytest
import torch

sys.path.insert(0, str(Path(__file__).parents[1] / "tools"))

import run_recursive_mas_tokenwise_real as runner

from ralfloop_agent.domains.recursive_mas_domain_training import _require_safe_runtime
from ralfloop_agent.domains.recursive_mas_profiles import MATH_PROFILE
from ralfloop_agent.domains.recursive_mas_tokenwise_real import (
    IdentityInner,
    RealTrajectoryConfig,
    ZeroGatedResidualInner,
    aggregate_group_retrieval,
    delta_regularization,
    final_h1b_gate,
    full_raw_teacher_forcing,
    identity_preferred,
    raw_record_valid,
    raw_trajectory_record,
    token_group,
    validation_constraints,
)


def _generated(raw: str = "not json", token_ids: list[int] | None = None, eos: bool = True):
    return {"raw": raw, "token_ids": token_ids or [7, 8, 9], "eos": eos}


def _record(raw: str = "not json", parsed=None):
    return raw_trajectory_record(
        case_id="case", view_id="original", prompt="prompt", generated=_generated(raw), parsed=parsed,
        model_revision="revision", tokenizer_hash="tokenizer", generation_config={"max_new_tokens": 512},
    )


def _metrics(*, overall=.9, content=.9, unseen=.8, cosine=.5, mse=.7, unique=.3):
    return {
        "mean_cosine_similarity": cosine, "mse": mse, "sequence_collapse": False, "nan_or_inf": False,
        "retrieval": {
            "overall": {"top5": overall, "unique_nearest_ratio": unique},
            "content": {"top5": content}, "unseen": {"top5": unseen},
        },
    }


def test_raw_output_preserved_before_parsing() -> None:
    record = _record("RAW invalid critic text", None)
    assert record["raw_critic_output"] == "RAW invalid critic text"
    assert record["parsed_critic_output"] is None
    assert record["raw_output_token_ids"] == [7, 8, 9]


def test_no_empty_object_substitution_and_parsed_invalid_retained() -> None:
    record = _record("invalid but nonempty", None)
    assert record["parse_valid"] is False
    assert raw_record_valid(record)
    assert record["raw_critic_output"] != "{}"


def test_empty_raw_excluded() -> None:
    record = _record("   ", None)
    assert record["excluded"]
    assert record["exclusion_reason"] == "empty_raw_output"
    assert not raw_record_valid(record)


def test_raw_teacher_forcing_mask_and_padding() -> None:
    value = full_raw_teacher_forcing(prompt_ids=[1, 2], raw_output_ids=[3, 4, 5], pad_token_id=0, multiple=4)
    assert value["full_ids"].tolist() == [[1, 2, 3, 4, 5, 0, 0, 0]]
    assert value["assistant_mask"].tolist() == [[False, False, True, True, True, False, False, False]]
    assert value["attention_mask"].tolist() == [[1, 1, 1, 1, 1, 0, 0, 0]]


def test_train_validation_split_isolation() -> None:
    splits = runner.cases_by_split()
    assert {case["id"] for case in splits["train"]}.isdisjoint(case["id"] for case in splits["validation"])
    assert len(splits["train"]) == 168 and len(splits["validation"]) == 36


def test_order_view_generation_preserves_content() -> None:
    case = runner.cases_by_split()["train"][0]
    reverse = runner.reordered(case, "reversed")
    seeded = runner.reordered(case, "seeded")
    assert reverse["question"] == seeded["question"] == case["question"]
    assert [item["rule_id"] for item in reverse["rules"]] == list(reversed([item["rule_id"] for item in case["rules"]]))
    assert {item["source_id"] for item in seeded["sources"]} == {item["source_id"] for item in case["sources"]}


def test_real_trajectory_cache_hash_code_present() -> None:
    source = inspect.getsource(runner.load_cache)
    assert "h1b_cache_hash_mismatch" in source
    assert "tensor_sha256" in source


@pytest.mark.parametrize(
    "token,decoded,expected",
    [(1, "<eos>", "special"), (2, "R_17", "identifier"), (3, "{", "structural"), (4, " argument", "content")],
)
def test_token_category_metrics(token: int, decoded: str, expected: str) -> None:
    assert token_group(token, decoded, special_ids={1}, identifier_ids={2}, structural_ids={3}) == expected


def test_seen_unseen_metrics() -> None:
    nearest = torch.tensor([[1, 4], [3, 2]])
    targets = torch.tensor([1, 2])
    result = aggregate_group_retrieval(nearest, targets, ["content", "identifier"], {1})
    assert result["seen"]["top1"] == 1.0
    assert result["unseen"]["top1"] == 0.0
    assert result["unseen"]["top5"] == 1.0


def test_identity_baseline_exact() -> None:
    value = torch.randn(2, 3, 4)
    assert torch.equal(IdentityInner()(value), value)


def test_zero_gated_residual_initialization_exact_identity() -> None:
    module = ZeroGatedResidualInner(hidden_size=4)
    value = torch.randn(2, 3, 4)
    assert float(module.alpha) == 0.0
    assert torch.equal(module(value), value)
    assert torch.count_nonzero(module.proj2.weight) == 0


def test_zero_gate_is_trainable() -> None:
    module = ZeroGatedResidualInner(hidden_size=4)
    loss = module(torch.randn(1, 2, 4)).sum()
    loss.backward()
    assert module.alpha_logit.grad is not None
    assert torch.isfinite(module.alpha_logit.grad)


def test_delta_regularization() -> None:
    module = ZeroGatedResidualInner(hidden_size=4)
    source = torch.randn(1, 2, 4)
    assert delta_regularization(module, source) > 0
    assert delta_regularization(IdentityInner(), source) == 0


def test_validation_real_checkpoint_selection_accepts_geometry() -> None:
    identity = _metrics(cosine=.2, mse=1.0)
    candidate = _metrics(overall=.89, content=.91, unseen=.78, cosine=.4, mse=.8)
    assert validation_constraints(candidate, identity)["passed"]


def test_retrieval_regression_rejected_despite_alignment() -> None:
    identity = _metrics(cosine=.2, mse=1.0)
    candidate = _metrics(overall=.7, content=.7, unseen=.5, cosine=.8, mse=.2)
    result = validation_constraints(candidate, identity)
    assert not result["passed"]
    assert result["classification"] == "alignment_loss_improves_but_token_geometry_regresses"


def test_freeze_required_before_final_a() -> None:
    source = inspect.getsource(runner.generate_final_a)
    assert "h1b_final_a_before_freeze" in source
    assert "h1b_frozen_manifest.json" in source


def test_h1b_gate() -> None:
    identity = _metrics(cosine=.2, mse=1.0)
    candidate = _metrics(overall=.88, content=.88, unseen=.76, cosine=.5, mse=.7)
    contract = {key: True for key in ("positionwise", "no_pooling", "shift", "assistant_mask", "eos", "padding")}
    assert final_h1b_gate(candidate, identity, contract)["passed"]


def test_identity_mapping_preferred() -> None:
    identity = _metrics(overall=.95, content=.95, unseen=.9, cosine=.5, mse=.5)
    candidates = [_metrics(overall=.8, content=.8, unseen=.7, cosine=.7, mse=.3)]
    assert identity_preferred(identity, candidates)


def test_no_outer23_training() -> None:
    source = Path("tools/run_recursive_mas_tokenwise_real.py").read_text(encoding="utf-8")
    assert '"outer23_trained": False' in source
    assert "outer23" not in inspect.getsource(runner.train_variant)


def test_reserve_b_not_semantically_read_or_executed() -> None:
    source = Path("tools/run_recursive_mas_tokenwise_real.py").read_text(encoding="utf-8")
    assert "generate_final_a" in source
    assert "RESERVE_B" not in inspect.getsource(runner.generate_corpus)
    assert "RESERVE_B" not in inspect.getsource(runner.evaluate_final_a)


def test_feature_flag_math_profile_telegram_gate() -> None:
    assert MATH_PROFILE.profile_id == "recursive_mas_math"
    assert 'os.getenv("RALF_RECURSIVE_DOMAIN_REASONING", "0") != "0"' in inspect.getsource(_require_safe_runtime)
    assert "approval" in Path("ralfloop_agent/domains/domain_approval_executor.py").read_text(encoding="utf-8").casefold()


def test_config_bounds() -> None:
    RealTrajectoryConfig().validate()
    with pytest.raises(ValueError, match="h1b_step_limit_invalid"):
        RealTrajectoryConfig(max_steps=401).validate()
