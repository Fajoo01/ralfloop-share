from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Mapping, Sequence

import torch
from torch import nn
import torch.nn.functional as functional

from .recursive_mas_domain_instruct_profile import LINK_DIMENSIONS


HIDDEN_SIZES = {"planner": 2048, "critic": 1536, "solver": 2048}


@dataclass(frozen=True)
class FinalTrainingConfig:
    max_steps: int = 150
    checkpoint_every: int = 10
    batch_size: int = 1
    gradient_accumulation: int = 4
    sequence_lengths: tuple[int, ...] = (128, 256, 512)
    mixed_precision: str = "float16"
    lambda_planner_structured: float = 1.0
    lambda_critic_structured: float = 1.0
    lambda_latent_alignment: float = 0.25
    lambda_solver_final_token_ce: float = 4.0
    early_stopping_patience: int = 3

    def validate(self) -> None:
        if self.max_steps > 150 or self.max_steps < 1:
            raise ValueError("instruct_micro_step_limit_invalid")
        if self.checkpoint_every != 10:
            raise ValueError("instruct_checkpoint_interval_invalid")
        if self.batch_size != 1 or self.gradient_accumulation < 1:
            raise ValueError("instruct_memory_strategy_invalid")
        if self.sequence_lengths != (128, 256, 512):
            raise ValueError("instruct_sequence_probe_order_invalid")
        final = self.lambda_solver_final_token_ce
        if final <= max(self.lambda_planner_structured, self.lambda_critic_structured, self.lambda_latent_alignment):
            raise ValueError("final_token_ce_must_dominate")


class NativeInnerAdapter(nn.Module):
    """Fresh RecursiveMAS ln_res_adapter; no checkpoint is loaded implicitly."""

    def __init__(self, hidden_size: int) -> None:
        super().__init__()
        self.proj1 = nn.Linear(hidden_size, hidden_size)
        self.act = nn.GELU()
        self.proj2 = nn.Linear(hidden_size, hidden_size)
        self.pre_ln = nn.LayerNorm(hidden_size)
        self.post_ln = nn.LayerNorm(hidden_size)

    def forward(self, value: torch.Tensor) -> torch.Tensor:
        hidden = self.pre_ln(value)
        output = self.proj2(self.act(self.proj1(hidden)))
        return self.post_ln(value + output)


class NativeCrossModelAdapter(nn.Module):
    """Fresh RecursiveMAS outer_ln_res_adapter; no Math/domain state is reused."""

    def __init__(self, in_dim: int, out_dim: int) -> None:
        super().__init__()
        hidden_dim = out_dim * 2
        self.proj1 = nn.Linear(in_dim, hidden_dim)
        self.act = nn.GELU()
        self.proj2 = nn.Linear(hidden_dim, out_dim)
        self.ln_source = nn.LayerNorm(in_dim)
        self.ln_target = nn.LayerNorm(out_dim)
        self.residual_proj = nn.Linear(in_dim, out_dim)

    def forward(self, value: torch.Tensor) -> torch.Tensor:
        hidden = self.ln_source(value)
        output = self.proj2(self.act(self.proj1(hidden)))
        return self.ln_target(output + self.residual_proj(value))


def initialize_fresh_adapters(
    *,
    hidden_sizes: Mapping[str, int] = HIDDEN_SIZES,
    link_dimensions: Mapping[str, tuple[int, int]] = LINK_DIMENSIONS,
    seed: int = 42,
) -> dict[str, nn.ModuleDict]:
    torch.manual_seed(seed)
    return {
        "inner": nn.ModuleDict({role: NativeInnerAdapter(int(size)) for role, size in hidden_sizes.items()}),
        "cross": nn.ModuleDict(
            {name: NativeCrossModelAdapter(int(dimensions[0]), int(dimensions[1])) for name, dimensions in link_dimensions.items()}
        ),
    }


def final_decode_graph() -> list[dict[str, Any]]:
    return [
        {
            "tensor_source": "critic_self_hidden",
            "adapter": "critic_inner",
            "tensor_destination": "critic_latent",
            "used_by_final_decode": True,
            "loss": "solver_final_token_ce+critic_structured",
        },
        {
            "tensor_source": "critic_latent",
            "adapter": "outer_23_critic_to_solver",
            "tensor_destination": "solver_hidden",
            "used_by_final_decode": True,
            "loss": "solver_final_token_ce+latent_alignment",
        },
        {
            "tensor_source": "solver_hidden",
            "adapter": "solver_inner",
            "tensor_destination": "solver_input_embeddings",
            "used_by_final_decode": True,
            "loss": "solver_final_token_ce",
        },
        {
            "tensor_source": "solver_self_hidden",
            "adapter": "outer_31_solver_to_planner",
            "tensor_destination": "next_round_planner_hidden",
            "used_by_final_decode": False,
            "loss": "next_round_only",
        },
        {
            "tensor_source": "solver_input_embeddings",
            "adapter": "frozen_solver_instruct",
            "tensor_destination": "final_token_logits",
            "used_by_final_decode": True,
            "loss": "solver_final_token_ce_response_tokens_only",
        },
    ]


def response_only_labels(prompt_length: int, target_ids: torch.Tensor) -> torch.Tensor:
    if target_ids.ndim != 2 or prompt_length < 1:
        raise ValueError("response_mask_shape_invalid")
    ignored = torch.full(
        (target_ids.shape[0], prompt_length),
        -100,
        dtype=torch.long,
        device=target_ids.device,
    )
    return torch.cat((ignored, target_ids.to(dtype=torch.long)), dim=1)


def final_output_token_ce(
    *,
    frozen_solver: nn.Module,
    critic_hidden: torch.Tensor,
    critic_inner: nn.Module,
    critic_to_solver: nn.Module,
    solver_inner: nn.Module,
    prefix_ids: torch.Tensor,
    suffix_ids: torch.Tensor,
    target_ids: torch.Tensor,
    sequence_length: int,
) -> tuple[torch.Tensor, dict[str, Any]]:
    if not all(value.ndim == 2 for value in (prefix_ids, suffix_ids, target_ids)):
        raise ValueError("final_token_id_shape_invalid")
    if not all(value.shape[0] == critic_hidden.shape[0] for value in (prefix_ids, suffix_ids, target_ids)):
        raise ValueError("final_token_batch_mismatch")
    embed = frozen_solver.get_input_embeddings()
    critic_latent = critic_inner(critic_hidden)
    solver_hidden = critic_to_solver(critic_latent)
    solver_latent = solver_inner(solver_hidden).to(dtype=embed.weight.dtype)
    prefix = embed(prefix_ids).to(dtype=embed.weight.dtype)
    suffix = embed(suffix_ids).to(dtype=embed.weight.dtype)
    response = embed(target_ids).to(dtype=embed.weight.dtype)
    prompt = torch.cat((prefix, solver_latent, suffix), dim=1)
    full = torch.cat((prompt, response), dim=1)
    if full.shape[1] > sequence_length:
        raise RuntimeError(f"sequence_length_exceeded:{full.shape[1]}:{sequence_length}")
    labels = response_only_labels(prompt.shape[1], target_ids)
    output = frozen_solver(inputs_embeds=full, use_cache=False, return_dict=True)
    logits = output.logits
    shift_logits = logits[:, :-1, :].contiguous().float()
    shift_labels = labels[:, 1:].contiguous()
    loss = functional.cross_entropy(shift_logits.reshape(-1, shift_logits.shape[-1]), shift_labels.reshape(-1), ignore_index=-100)
    return loss, {
        "prompt_tokens": int(prompt.shape[1]),
        "response_tokens": int(target_ids.shape[1]),
        "masked_input_tokens": int((labels == -100).sum()),
        "supervised_response_tokens": int((labels != -100).sum()),
        "outer_31_used": False,
    }


def combine_supervision_losses(
    losses: Mapping[str, torch.Tensor],
    config: FinalTrainingConfig = FinalTrainingConfig(),
) -> torch.Tensor:
    config.validate()
    required = {"planner_structured", "critic_structured", "latent_alignment", "solver_final_token_ce"}
    if set(losses) != required:
        raise ValueError("instruct_supervision_loss_set_invalid")
    return (
        config.lambda_planner_structured * losses["planner_structured"]
        + config.lambda_critic_structured * losses["critic_structured"]
        + config.lambda_latent_alignment * losses["latent_alignment"]
        + config.lambda_solver_final_token_ce * losses["solver_final_token_ce"]
    )


def gradient_norm(module: nn.Module) -> float:
    total = 0.0
    for parameter in module.parameters():
        if parameter.grad is not None:
            total += float(torch.sum(parameter.grad.detach().float().square()))
    return total**0.5


def final_gradient_report(*, critic_inner: nn.Module, critic_to_solver: nn.Module, solver_inner: nn.Module) -> dict[str, Any]:
    norms = {
        "critic_inner": gradient_norm(critic_inner),
        "critic_to_solver": gradient_norm(critic_to_solver),
        "solver_inner": gradient_norm(solver_inner),
    }
    return {
        "norms": norms,
        "required_path_reached": norms["critic_to_solver"] > 0 and norms["solver_inner"] > 0,
        "outer_31_in_final_path": False,
    }


def probe_sequence_lengths(
    probe: Callable[[int], Any],
    lengths: Sequence[int] = (128, 256, 512),
    *,
    empty_cuda_cache: Callable[[], None] | None = None,
) -> dict[str, Any]:
    attempted: list[int] = []
    successful: list[int] = []
    oom_at: int | None = None
    for length in lengths:
        attempted.append(int(length))
        try:
            probe(int(length))
        except torch.cuda.OutOfMemoryError:
            oom_at = int(length)
            if empty_cuda_cache is not None:
                empty_cuda_cache()
            break
        successful.append(int(length))
    if not successful:
        raise RuntimeError("final_token_ce_vram_insufficient")
    return {"attempted": attempted, "successful": successful, "selected": successful[-1], "oom_at": oom_at}


def training_preflight(*, direct_gate: Mapping[str, Any], compatibility: Mapping[str, Any]) -> dict[str, Any]:
    if not compatibility.get("ok"):
        raise RuntimeError("instruct_model_shape_incompatible")
    if not direct_gate.get("passed"):
        raise RuntimeError("instruct_base_insufficient")
    config = FinalTrainingConfig()
    config.validate()
    return {
        "ok": True,
        "profile_id": "recursive_mas_domain_instruct_v1",
        "base_models_frozen": True,
        "gpu_stagewise": True,
        "simultaneous_base_models": 1,
        "final_decode_directly_supervised": True,
        "config": config,
    }


def solver_micro_gate(metrics: Mapping[str, Any]) -> dict[str, Any]:
    passed = bool(
        int(metrics.get("valid_count") or 0) == 8
        and float(metrics.get("schema_validity") or 0.0) == 1.0
        and float(metrics.get("rule_accuracy") or 0.0) == 1.0
        and float(metrics.get("source_accuracy") or 0.0) == 1.0
    )
    return {"passed": passed, "criterion": "8/8_schema_rule_source"}


def pipeline_micro_gate(metrics: Mapping[str, Any]) -> dict[str, Any]:
    passed = bool(
        int(metrics.get("valid_count") or 0) >= 7
        and float(metrics.get("schema_validity") or 0.0) == 1.0
        and float(metrics.get("rule_accuracy") or 0.0) >= 0.875
        and float(metrics.get("source_accuracy") or 0.0) >= 0.875
    )
    return {"passed": passed, "criterion": "7/8_schema100_rule87.5_source87.5"}
