from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
from pathlib import Path
from typing import Any, Mapping, Sequence

import torch
from torch import nn
import torch.nn.functional as functional

from .recursive_mas_domain_instruct_training import NativeInnerAdapter


PROFILE_ID = "recursive_mas_domain_tokenwise_inner_v1"
CRITIC_HIDDEN_SIZE = 1536


@dataclass(frozen=True)
class TokenwiseInnerConfig:
    seed: int = 73129
    micro_steps: int = 100
    bounded_steps: int = 300
    checkpoint_every_micro: int = 10
    checkpoint_every_bounded: int = 25
    evaluate_every_micro: int = 10
    evaluate_every_bounded: int = 25
    batch_size: int = 1
    gradient_accumulation: int = 4
    micro_learning_rate: float = 2e-4
    bounded_learning_rate: float = 5e-4
    gradient_clip: float = 1.0
    cosine_weight: float = 1.0
    mse_weight: float = 0.1
    validation_patience: int = 4
    retrieval_positions_per_category: int = 64

    def validate(self) -> None:
        if self.micro_steps > 100 or self.bounded_steps > 300:
            raise ValueError("tokenwise_step_limit_invalid")
        if self.batch_size != 1 or self.gradient_accumulation < 1:
            raise ValueError("tokenwise_batch_contract_invalid")
        if self.cosine_weight != 1.0 or self.mse_weight != 0.1:
            raise ValueError("tokenwise_loss_contract_invalid")
        if self.gradient_clip != 1.0:
            raise ValueError("tokenwise_gradient_clip_invalid")


def initialize_critic_inner_tokenwise(seed: int = 73129) -> NativeInnerAdapter:
    torch.manual_seed(seed)
    module = NativeInnerAdapter(CRITIC_HIDDEN_SIZE).to(dtype=torch.float32)
    if {parameter.dtype for parameter in module.parameters()} != {torch.float32}:
        raise RuntimeError("tokenwise_adapter_not_fp32")
    return module


def fp32_adamw(module: nn.Module, *, learning_rate: float) -> torch.optim.AdamW:
    parameters = list(module.parameters())
    if not parameters or any(parameter.dtype != torch.float32 for parameter in parameters):
        raise ValueError("tokenwise_optimizer_requires_fp32_parameters")
    optimizer = torch.optim.AdamW(parameters, lr=learning_rate, betas=(0.9, 0.95))
    if any(parameter.dtype != torch.float32 for group in optimizer.param_groups for parameter in group["params"]):
        raise RuntimeError("tokenwise_optimizer_master_weights_not_fp32")
    return optimizer


def assistant_mask_from_prefix(full_ids: torch.Tensor, prompt_ids: torch.Tensor) -> torch.Tensor:
    if full_ids.ndim != 2 or prompt_ids.ndim != 2 or full_ids.shape[0] != prompt_ids.shape[0]:
        raise ValueError("tokenwise_id_shape_invalid")
    if prompt_ids.shape[1] >= full_ids.shape[1]:
        raise ValueError("tokenwise_assistant_span_missing")
    if not torch.equal(full_ids[:, : prompt_ids.shape[1]], prompt_ids):
        raise ValueError("tokenwise_chat_prefix_mismatch")
    mask = torch.zeros_like(full_ids, dtype=torch.bool)
    mask[:, prompt_ids.shape[1] :] = True
    return mask


def build_positionwise_pairs(
    *,
    hidden_states: torch.Tensor,
    full_ids: torch.Tensor,
    input_embeddings: nn.Module,
    attention_mask: torch.Tensor,
    assistant_mask: torch.Tensor,
) -> dict[str, torch.Tensor]:
    if hidden_states.ndim != 3 or full_ids.ndim != 2:
        raise ValueError("tokenwise_hidden_or_id_shape_invalid")
    if hidden_states.shape[:2] != full_ids.shape:
        raise ValueError("tokenwise_hidden_id_length_mismatch")
    if attention_mask.shape != full_ids.shape or assistant_mask.shape != full_ids.shape:
        raise ValueError("tokenwise_mask_shape_invalid")
    source_hidden = hidden_states[:, :-1, :]
    with torch.no_grad():
        target_embed = input_embeddings(full_ids)[:, 1:, :]
    pair_mask = assistant_mask[:, 1:].bool() & attention_mask[:, :-1].bool()
    target_ids = full_ids[:, 1:]
    if not pair_mask.any():
        raise ValueError("tokenwise_pair_mask_empty")
    return {
        "source_hidden": source_hidden,
        "target_embed": target_embed,
        "target_ids": target_ids,
        "pair_mask": pair_mask,
    }


def selected_positions(value: torch.Tensor, pair_mask: torch.Tensor) -> torch.Tensor:
    if value.ndim == 3 and value.shape[:2] == pair_mask.shape:
        return value[pair_mask]
    if value.ndim == 2 and value.shape == pair_mask.shape:
        return value[pair_mask]
    raise ValueError("tokenwise_selected_position_shape_invalid")


def masked_positionwise_loss(
    prediction: torch.Tensor,
    target: torch.Tensor,
    pair_mask: torch.Tensor,
    *,
    cosine_weight: float = 1.0,
    mse_weight: float = 0.1,
) -> tuple[torch.Tensor, dict[str, torch.Tensor]]:
    predicted = selected_positions(prediction.float(), pair_mask)
    expected = selected_positions(target.float(), pair_mask)
    if predicted.shape != expected.shape or predicted.numel() == 0:
        raise ValueError("tokenwise_selected_pair_invalid")
    cosine = 1.0 - functional.cosine_similarity(predicted, expected, dim=-1).mean()
    mse = functional.mse_loss(predicted, expected)
    total = cosine_weight * cosine + mse_weight * mse
    return total, {"cosine_loss": cosine, "mse": mse, "selected_positions": pair_mask.sum()}


def position_statistics(prediction: torch.Tensor, target: torch.Tensor) -> dict[str, float]:
    if prediction.ndim != 2 or target.shape != prediction.shape or prediction.shape[0] < 1:
        raise ValueError("tokenwise_metric_shape_invalid")
    cosine = functional.cosine_similarity(prediction.float(), target.float(), dim=-1)
    adjacent = (
        functional.cosine_similarity(prediction[1:].float(), prediction[:-1].float(), dim=-1).mean()
        if prediction.shape[0] > 1
        else torch.tensor(0.0)
    )
    variance = prediction.float().var(dim=0, unbiased=False).mean()
    return {
        "mean_cosine_similarity": float(cosine.mean()),
        "median_cosine_similarity": float(cosine.median()),
        "p05_cosine_similarity": float(torch.quantile(cosine, 0.05)),
        "mse": float(functional.mse_loss(prediction.float(), target.float())),
        "adjacent_position_cosine": float(adjacent),
        "position_variance": float(variance),
    }


def nearest_embedding_tokens(
    prediction: torch.Tensor,
    embedding_weight: torch.Tensor,
    *,
    top_k: int = 5,
    position_chunk: int = 32,
    vocab_chunk: int = 4096,
) -> torch.Tensor:
    if prediction.ndim != 2 or embedding_weight.ndim != 2 or prediction.shape[1] != embedding_weight.shape[1]:
        raise ValueError("tokenwise_retrieval_shape_invalid")
    if top_k < 1 or top_k > embedding_weight.shape[0]:
        raise ValueError("tokenwise_retrieval_k_invalid")
    device = prediction.device
    normalized_prediction = functional.normalize(prediction.float(), dim=-1)
    output: list[torch.Tensor] = []
    for start in range(0, normalized_prediction.shape[0], position_chunk):
        values = normalized_prediction[start : start + position_chunk]
        best_scores = torch.full((values.shape[0], top_k), -torch.inf, device=device)
        best_ids = torch.full((values.shape[0], top_k), -1, dtype=torch.long, device=device)
        for vocab_start in range(0, embedding_weight.shape[0], vocab_chunk):
            chunk = embedding_weight[vocab_start : vocab_start + vocab_chunk].to(device=device, dtype=torch.float32)
            scores = values @ functional.normalize(chunk, dim=-1).T
            local_scores, local_ids = scores.topk(min(top_k, scores.shape[1]), dim=-1)
            local_ids = local_ids + vocab_start
            merged_scores = torch.cat((best_scores, local_scores), dim=-1)
            merged_ids = torch.cat((best_ids, local_ids), dim=-1)
            best_scores, order = merged_scores.topk(top_k, dim=-1)
            best_ids = merged_ids.gather(1, order)
            del chunk, scores, local_scores, local_ids, merged_scores, merged_ids, order
        output.append(best_ids.cpu())
    return torch.cat(output, dim=0)


def retrieval_statistics(nearest_ids: torch.Tensor, target_ids: torch.Tensor) -> dict[str, float]:
    targets = target_ids.detach().cpu().long().reshape(-1)
    if nearest_ids.ndim != 2 or nearest_ids.shape[0] != targets.shape[0]:
        raise ValueError("tokenwise_retrieval_result_shape_invalid")
    top1 = nearest_ids[:, 0].eq(targets)
    top5 = nearest_ids.eq(targets[:, None]).any(dim=1)
    unique_ratio = float(nearest_ids[:, 0].unique().numel() / max(1, nearest_ids.shape[0]))
    return {
        "top1_token_accuracy": float(top1.float().mean()),
        "top5_token_accuracy": float(top5.float().mean()),
        "unique_nearest_token_ratio": unique_ratio,
    }


def collapse_report(metrics: Mapping[str, float]) -> dict[str, Any]:
    collapsed = bool(
        float(metrics.get("position_variance", 0.0)) < 1e-7
        or float(metrics.get("adjacent_position_cosine", 0.0)) > 0.99995
        or float(metrics.get("unique_nearest_token_ratio", 0.0)) < 0.01
    )
    return {"sequence_collapse": collapsed, "same_vector_collapse": collapsed}


def generation_layout(*, prefix_length: int, latent_length: int, suffix_length: int) -> dict[str, Any]:
    if min(prefix_length, latent_length, suffix_length) < 0 or latent_length < 1:
        raise ValueError("tokenwise_generation_layout_invalid")
    total = prefix_length + latent_length + suffix_length
    positions = list(range(total))
    return {
        "prefix": [0, prefix_length],
        "latent": [prefix_length, prefix_length + latent_length],
        "suffix": [prefix_length + latent_length, total],
        "attention_mask_length": total,
        "inputs_embeds_length": total,
        "position_ids": positions,
        "position_ids_monotonic": all(right == left + 1 for left, right in zip(positions, positions[1:])),
        "generation_boundary": total,
    }


def faithful_one_round_latent(critic_inner: nn.Module, outer23: nn.Module, critic_trajectory: torch.Tensor) -> torch.Tensor:
    """One-round upstream path: sender inner then cross-model adapter; no solver inner."""
    return outer23(critic_inner(critic_trajectory))


def h1_gate(fresh: Mapping[str, float], trained: Mapping[str, float], contract: Mapping[str, Any]) -> dict[str, Any]:
    cosine_improvement = (float(fresh["cosine_loss"]) - float(trained["cosine_loss"])) / max(
        abs(float(fresh["cosine_loss"])), 1e-12
    )
    mse_improvement = (float(fresh["mse"]) - float(trained["mse"])) / max(abs(float(fresh["mse"])), 1e-12)
    passed = bool(
        cosine_improvement >= 0.20
        and mse_improvement >= 0.20
        and float(trained["top5_token_accuracy"]) > float(fresh["top5_token_accuracy"])
        and not bool(trained.get("sequence_collapse"))
        and all(
            bool(contract.get(key))
            for key in ("shift_t_to_t_plus_1", "assistant_only", "eos_included", "padding_excluded", "order_preserved", "no_pooling")
        )
        and not bool(trained.get("nan_or_inf"))
    )
    return {
        "passed": passed,
        "classification": "faithful_tokenwise_inner_alignment_passed" if passed else (
            "tokenwise_alignment_collapsed" if trained.get("sequence_collapse") else "tokenwise_alignment_not_trainable"
        ),
        "cosine_improvement": cosine_improvement,
        "mse_improvement": mse_improvement,
    }


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def stable_sha256(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def save_tokenwise_checkpoint(module: nn.Module, path: Path, metadata: Mapping[str, Any]) -> dict[str, Any]:
    if any(parameter.dtype != torch.float32 for parameter in module.parameters()):
        raise ValueError("tokenwise_checkpoint_not_fp32")
    path.parent.mkdir(parents=True, exist_ok=True)
    state = {name: value.detach().cpu() for name, value in module.state_dict().items()}
    torch.save({"state_dict": state, "metadata": dict(metadata)}, path)
    record = {
        **dict(metadata),
        "path": str(path),
        "sha256": sha256_file(path),
        "parameters": sum(parameter.numel() for parameter in module.parameters()),
        "shape": {name: list(value.shape) for name, value in state.items()},
        "dtype": sorted({str(value.dtype) for value in state.values()}),
    }
    path.with_suffix(".json").write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return record


def load_tokenwise_checkpoint(module: nn.Module, path: Path, expected_sha256: str) -> Mapping[str, Any]:
    if sha256_file(path) != expected_sha256:
        raise RuntimeError("tokenwise_checkpoint_hash_mismatch")
    payload = torch.load(path, map_location="cpu", weights_only=True)
    module.load_state_dict(payload["state_dict"], strict=True)
    return payload["metadata"]


def contract_manifest(config: TokenwiseInnerConfig = TokenwiseInnerConfig()) -> dict[str, Any]:
    config.validate()
    return {
        "profile_id": PROFILE_ID,
        "registered": False,
        "enabled": False,
        "contract": "hidden_states[:,:-1] -> critic_inner_tokenwise_v1 -> input_embeddings(full_ids)[:,1:]",
        "pair_mask": "assistant_mask[:,1:] & attention_mask[:,:-1].bool()",
        "pooling": None,
        "solver_inner_in_one_round_path": False,
        "outer31_present": False,
        "training_config": asdict(config),
    }
