from __future__ import annotations

from types import SimpleNamespace

import pytest

torch = pytest.importorskip("torch")
from torch import nn

from ralfloop_agent.domains.recursive_mas_domain_instruct_training import (
    FinalTrainingConfig,
    combine_supervision_losses,
    final_decode_graph,
    final_gradient_report,
    final_output_token_ce,
    initialize_fresh_adapters,
    pipeline_micro_gate,
    probe_sequence_lengths,
    response_only_labels,
    solver_micro_gate,
    training_preflight,
)


class ToyFrozenSolver(nn.Module):
    def __init__(self, vocab_size: int = 17, hidden_size: int = 6) -> None:
        super().__init__()
        self.embedding = nn.Embedding(vocab_size, hidden_size)
        self.head = nn.Linear(hidden_size, vocab_size, bias=False)

    def get_input_embeddings(self):
        return self.embedding

    def forward(self, *, inputs_embeds, **kwargs):
        return SimpleNamespace(logits=self.head(inputs_embeds.cumsum(dim=1)))


def test_final_token_ce_weight_dominates_all_surrogate_losses():
    config = FinalTrainingConfig()
    config.validate()
    assert config.lambda_solver_final_token_ce > config.lambda_planner_structured
    assert config.lambda_solver_final_token_ce > config.lambda_critic_structured
    assert config.lambda_solver_final_token_ce > config.lambda_latent_alignment
    losses = {name: torch.tensor(1.0) for name in ("planner_structured", "critic_structured", "latent_alignment", "solver_final_token_ce")}
    assert float(combine_supervision_losses(losses, config)) == 6.25


def test_new_adapters_are_fresh_and_use_instruct_dimensions_without_math_state():
    dimensions = {"planner": 4, "critic": 3, "solver": 4}
    links = {"outer_12": (4, 3), "outer_23": (3, 4), "outer_31": (4, 4)}
    first = initialize_fresh_adapters(hidden_sizes=dimensions, link_dimensions=links, seed=1)
    second = initialize_fresh_adapters(hidden_sizes=dimensions, link_dimensions=links, seed=2)
    assert set(first["inner"]) == {"planner", "critic", "solver"}
    assert set(first["cross"]) == {"outer_12", "outer_23", "outer_31"}
    assert not torch.equal(first["inner"]["solver"].proj1.weight, second["inner"]["solver"].proj1.weight)


def test_outer31_is_next_round_only_not_final_decoder():
    graph = final_decode_graph()
    outer31 = next(item for item in graph if item["adapter"].startswith("outer_31"))
    final_edges = [item["adapter"] for item in graph if item["used_by_final_decode"]]
    assert outer31["used_by_final_decode"] is False
    assert "outer_23_critic_to_solver" in final_edges
    assert "solver_inner" in final_edges


def test_response_mask_supervises_only_target_tokens():
    target = torch.tensor([[4, 5, 6]])
    labels = response_only_labels(4, target)
    assert labels.tolist() == [[-100, -100, -100, -100, 4, 5, 6]]


def test_final_decode_loss_backpropagates_through_solver_inner_and_outer23():
    solver = ToyFrozenSolver()
    for parameter in solver.parameters():
        parameter.requires_grad = False
    critic_inner = nn.Linear(3, 3)
    outer23 = nn.Linear(3, 6)
    solver_inner = nn.Linear(6, 6)
    loss, diagnostics = final_output_token_ce(
        frozen_solver=solver,
        critic_hidden=torch.randn(1, 2, 3),
        critic_inner=critic_inner,
        critic_to_solver=outer23,
        solver_inner=solver_inner,
        prefix_ids=torch.tensor([[1, 2]]),
        suffix_ids=torch.tensor([[3]]),
        target_ids=torch.tensor([[4, 5, 6]]),
        sequence_length=16,
    )
    loss.backward()
    report = final_gradient_report(critic_inner=critic_inner, critic_to_solver=outer23, solver_inner=solver_inner)
    assert diagnostics["masked_input_tokens"] == diagnostics["prompt_tokens"]
    assert diagnostics["supervised_response_tokens"] == 3
    assert diagnostics["outer_31_used"] is False
    assert report["required_path_reached"] is True
    assert report["norms"]["critic_inner"] > 0
    assert all(parameter.grad is None for parameter in solver.parameters())


def test_final_decode_rejects_sequence_length_that_cannot_hold_contract():
    solver = ToyFrozenSolver()
    with pytest.raises(RuntimeError, match="sequence_length_exceeded"):
        final_output_token_ce(
            frozen_solver=solver,
            critic_hidden=torch.randn(1, 2, 3),
            critic_inner=nn.Linear(3, 3),
            critic_to_solver=nn.Linear(3, 6),
            solver_inner=nn.Linear(6, 6),
            prefix_ids=torch.tensor([[1, 2]]),
            suffix_ids=torch.tensor([[3]]),
            target_ids=torch.tensor([[4, 5, 6]]),
            sequence_length=4,
        )


def test_oom_sequence_probe_runs_128_then_256_then_512_and_keeps_last_fit():
    attempted = []

    def probe(length):
        attempted.append(length)
        if length == 512:
            raise torch.cuda.OutOfMemoryError("synthetic")

    result = probe_sequence_lengths(probe, empty_cuda_cache=lambda: None)
    assert attempted == [128, 256, 512]
    assert result == {"attempted": [128, 256, 512], "successful": [128, 256], "selected": 256, "oom_at": 512}


def test_training_preflight_stops_on_failed_direct_solver_gate():
    with pytest.raises(RuntimeError, match="instruct_base_insufficient"):
        training_preflight(
            direct_gate={"passed": False, "classification": "instruct_base_insufficient"},
            compatibility={"ok": True},
        )


def test_solver_micro_overfit_is_judged_on_outputs_not_loss():
    failed = solver_micro_gate({"valid_count": 0, "schema_validity": 0, "rule_accuracy": 0, "source_accuracy": 0, "loss_final": 0.01})
    passed = solver_micro_gate({"valid_count": 8, "schema_validity": 1, "rule_accuracy": 1, "source_accuracy": 1})
    assert failed["passed"] is False
    assert passed["passed"] is True


def test_pipeline_micro_overfit_requires_output_gate():
    failed = pipeline_micro_gate({"valid_count": 8, "schema_validity": 0.875, "rule_accuracy": 1, "source_accuracy": 1})
    passed = pipeline_micro_gate({"valid_count": 7, "schema_validity": 1, "rule_accuracy": 0.875, "source_accuracy": 0.875})
    assert failed["passed"] is False
    assert passed["passed"] is True
