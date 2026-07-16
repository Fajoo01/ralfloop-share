from __future__ import annotations

import inspect
from pathlib import Path

import pytest
import torch
from torch import nn

from ralfloop_agent.domains.recursive_mas_domain_training import _require_safe_runtime
from ralfloop_agent.domains.recursive_mas_profiles import MATH_PROFILE
from ralfloop_agent.domains.recursive_mas_tokenwise_inner import (
    assistant_mask_from_prefix,
    build_positionwise_pairs,
    collapse_report,
    contract_manifest,
    faithful_one_round_latent,
    fp32_adamw,
    generation_layout,
    h1_gate,
    initialize_critic_inner_tokenwise,
    load_tokenwise_checkpoint,
    masked_positionwise_loss,
    nearest_embedding_tokens,
    retrieval_statistics,
    save_tokenwise_checkpoint,
)


class Embedding(nn.Module):
    def __init__(self) -> None:
        super().__init__()
        self.weight = nn.Parameter(torch.eye(8, 4)[:8])

    def forward(self, ids: torch.Tensor) -> torch.Tensor:
        return self.weight[ids]


def _pairs(*, padding: bool = False):
    full = torch.tensor([[0, 1, 2, 3, 4, 5]])
    prompt = torch.tensor([[0, 1, 2]])
    attention = torch.ones_like(full)
    if padding:
        attention[0, -1] = 0
    assistant = assistant_mask_from_prefix(full, prompt)
    assistant &= attention.bool()
    hidden = torch.arange(24, dtype=torch.float32).reshape(1, 6, 4)
    return build_positionwise_pairs(
        hidden_states=hidden,
        full_ids=full,
        input_embeddings=Embedding(),
        attention_mask=attention,
        assistant_mask=assistant,
    ), hidden, assistant


def test_t_to_t_plus_one_shift_and_positionwise_target() -> None:
    pairs, hidden, _ = _pairs()
    assert torch.equal(pairs["source_hidden"], hidden[:, :-1])
    assert pairs["target_ids"].tolist() == [[1, 2, 3, 4, 5]]
    assert pairs["pair_mask"].tolist() == [[False, False, True, True, True]]
    assert torch.equal(pairs["target_embed"][0, 2], Embedding()(torch.tensor([3]))[0])


def test_assistant_mask_includes_eos_and_excludes_prompt() -> None:
    pairs, _, assistant = _pairs()
    assert assistant.tolist() == [[False, False, False, True, True, True]]
    assert pairs["pair_mask"][0, -1]


def test_padding_is_excluded() -> None:
    pairs, _, _ = _pairs(padding=True)
    assert not pairs["pair_mask"][0, -1]
    assert int(pairs["pair_mask"].sum()) == 2


def test_prefix_mismatch_rejected() -> None:
    with pytest.raises(ValueError, match="tokenwise_chat_prefix_mismatch"):
        assistant_mask_from_prefix(torch.tensor([[1, 2, 3]]), torch.tensor([[1, 4]]))


def test_positionwise_loss_ignores_unselected_values() -> None:
    target = torch.randn(1, 5, 4)
    prediction = target.clone()
    prediction[:, :2] = 999
    mask = torch.tensor([[False, False, True, True, True]])
    total, parts = masked_positionwise_loss(prediction, target, mask)
    assert total.item() == pytest.approx(0.0, abs=1e-7)
    assert int(parts["selected_positions"]) == 3


def test_no_pooling_and_variable_sequence_length_contract() -> None:
    manifest = contract_manifest()
    assert manifest["pooling"] is None
    assert "mean" not in inspect.getsource(build_positionwise_pairs)
    for length in (3, 7, 19):
        prediction = torch.randn(1, length, 4)
        target = torch.randn_like(prediction)
        mask = torch.ones((1, length), dtype=torch.bool)
        _, parts = masked_positionwise_loss(prediction, target, mask)
        assert int(parts["selected_positions"]) == length


def test_order_preservation() -> None:
    pairs, hidden, _ = _pairs()
    selected = pairs["source_hidden"][pairs["pair_mask"]]
    assert torch.equal(selected, hidden[0, 2:5])


def test_nearest_token_metric() -> None:
    embeddings = torch.eye(6)
    target_ids = torch.tensor([1, 4])
    prediction = embeddings[target_ids]
    nearest = nearest_embedding_tokens(prediction, embeddings, top_k=5, position_chunk=1, vocab_chunk=2)
    metrics = retrieval_statistics(nearest, target_ids)
    assert metrics["top1_token_accuracy"] == 1.0
    assert metrics["top5_token_accuracy"] == 1.0


def test_collapse_detection() -> None:
    report = collapse_report({"position_variance": 0.0, "adjacent_position_cosine": 1.0, "unique_nearest_token_ratio": 0.01})
    assert report["sequence_collapse"]
    assert not collapse_report({"position_variance": 0.1, "adjacent_position_cosine": 0.5, "unique_nearest_token_ratio": 0.5})["sequence_collapse"]


def test_fp32_adapter_optimizer() -> None:
    module = initialize_critic_inner_tokenwise()
    optimizer = fp32_adamw(module, learning_rate=2e-4)
    assert all(parameter.dtype == torch.float32 for group in optimizer.param_groups for parameter in group["params"])
    module.half()
    with pytest.raises(ValueError, match="tokenwise_optimizer_requires_fp32_parameters"):
        fp32_adamw(module, learning_rate=2e-4)


def test_save_reload(tmp_path: Path) -> None:
    module = initialize_critic_inner_tokenwise()
    path = tmp_path / "critic_inner_tokenwise_v1.pt"
    record = save_tokenwise_checkpoint(module, path, {"step": 10})
    other = initialize_critic_inner_tokenwise(seed=9)
    metadata = load_tokenwise_checkpoint(other, path, record["sha256"])
    assert metadata["step"] == 10
    for left, right in zip(module.parameters(), other.parameters()):
        assert torch.equal(left, right)


def test_solver_inner_bypassed_and_outer31_excluded() -> None:
    critic_inner = nn.Identity()
    outer23 = nn.Linear(3, 4, bias=False)
    trajectory = torch.randn(1, 5, 3)
    result = faithful_one_round_latent(critic_inner, outer23, trajectory)
    assert result.shape == (1, 5, 4)
    source = inspect.getsource(faithful_one_round_latent)
    assert "solver_inner" not in source
    assert contract_manifest()["outer31_present"] is False


def test_latent_attention_mask_position_ids_and_generation_boundary() -> None:
    layout = generation_layout(prefix_length=11, latent_length=16, suffix_length=7)
    assert layout["inputs_embeds_length"] == layout["attention_mask_length"] == 34
    assert layout["position_ids_monotonic"]
    assert layout["latent"] == [11, 27]
    assert layout["generation_boundary"] == 34


def test_h1_gate() -> None:
    fresh = {"cosine_loss": 1.0, "mse": 1.0, "top5_token_accuracy": 0.1}
    trained = {"cosine_loss": 0.7, "mse": 0.7, "top5_token_accuracy": 0.2, "sequence_collapse": False, "nan_or_inf": False}
    contract = {
        "shift_t_to_t_plus_1": True,
        "assistant_only": True,
        "eos_included": True,
        "padding_excluded": True,
        "order_preserved": True,
        "no_pooling": True,
    }
    assert h1_gate(fresh, trained, contract)["passed"]


def test_reserve_b_not_read_or_executed_by_module() -> None:
    source = inspect.getsource(__import__("ralfloop_agent.domains.recursive_mas_tokenwise_inner", fromlist=["*"]))
    assert "reserve_b.jsonl" not in source


def test_feature_flag_math_profile_and_telegram_gate_unchanged() -> None:
    assert MATH_PROFILE.profile_id == "recursive_mas_math"
    assert 'os.getenv("RALF_RECURSIVE_DOMAIN_REASONING", "0") != "0"' in inspect.getsource(_require_safe_runtime)
    telegram = Path("ralfloop_agent/domains/domain_approval_executor.py").read_text(encoding="utf-8")
    assert "approval" in telegram.casefold()
