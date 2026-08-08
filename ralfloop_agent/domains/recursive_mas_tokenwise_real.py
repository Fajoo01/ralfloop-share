from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
import math
from pathlib import Path
import re
from typing import Any, Mapping, Sequence

import torch
from torch import nn
import torch.nn.functional as functional

from .recursive_mas_tokenwise_inner import CRITIC_HIDDEN_SIZE, stable_sha256


PROFILE_ID = "recursive_mas_domain_tokenwise_real_v1"
TOKEN_GROUPS = ("structural", "identifier", "content", "special")


@dataclass(frozen=True)
class RealTrajectoryConfig:
    seed: int = 91427
    max_steps: int = 400
    micro_steps: int = 20
    evaluate_every: int = 20
    checkpoint_every: int = 20
    gradient_accumulation: int = 4
    mixed_gradient_accumulation: int = 5
    positions_per_record: int = 128
    learning_rate: float = 1e-4
    gradient_clip: float = 1.0
    cosine_weight: float = 1.0
    mse_weight: float = 0.1
    delta_weight: float = 1e-5
    max_new_tokens: int = 512

    def validate(self) -> None:
        if self.max_steps > 400 or self.micro_steps != 20:
            raise ValueError("h1b_step_limit_invalid")
        if self.evaluate_every != 20 or self.checkpoint_every != 20:
            raise ValueError("h1b_interval_invalid")
        if self.gradient_accumulation < 4 or self.mixed_gradient_accumulation != 5:
            raise ValueError("h1b_accumulation_invalid")
        if self.cosine_weight != 1.0 or self.mse_weight != 0.1:
            raise ValueError("h1b_loss_contract_invalid")


class IdentityInner(nn.Module):
    def forward(self, value: torch.Tensor) -> torch.Tensor:
        return value


class ZeroGatedResidualInner(nn.Module):
    """Exact identity at init; bounded trainable gate; zero output projection."""

    def __init__(self, hidden_size: int = CRITIC_HIDDEN_SIZE) -> None:
        super().__init__()
        self.pre_ln = nn.LayerNorm(hidden_size)
        self.proj1 = nn.Linear(hidden_size, hidden_size)
        self.act = nn.GELU()
        self.proj2 = nn.Linear(hidden_size, hidden_size)
        self.alpha_logit = nn.Parameter(torch.zeros((), dtype=torch.float32))
        nn.init.zeros_(self.proj2.weight)
        nn.init.zeros_(self.proj2.bias)

    @property
    def alpha(self) -> torch.Tensor:
        return torch.tanh(self.alpha_logit)

    def delta(self, value: torch.Tensor) -> torch.Tensor:
        normalized = self.pre_ln(value)
        # Nonzero fixed residual lets alpha learn despite zero output projection.
        return normalized + self.proj2(self.act(self.proj1(normalized)))

    def forward(self, value: torch.Tensor) -> torch.Tensor:
        return value + self.alpha * self.delta(value)


def initialize_zero_gated(seed: int = 91427) -> ZeroGatedResidualInner:
    torch.manual_seed(seed)
    return ZeroGatedResidualInner().to(dtype=torch.float32)


def raw_trajectory_record(
    *,
    case_id: str,
    view_id: str,
    prompt: str,
    generated: Mapping[str, Any],
    parsed: Mapping[str, Any] | None,
    model_revision: str,
    tokenizer_hash: str,
    generation_config: Mapping[str, Any],
) -> dict[str, Any]:
    raw = str(generated.get("raw") or "")
    token_ids = [int(value) for value in generated.get("token_ids") or []]
    eos = bool(generated.get("eos"))
    empty = not raw.strip()
    return {
        "case_id": str(case_id),
        "view_id": str(view_id),
        "input_hash": stable_sha256({"case_id": case_id, "view_id": view_id, "prompt": prompt}),
        "prompt_hash": hashlib.sha256(prompt.encode()).hexdigest(),
        "raw_critic_output": raw,
        "raw_output_hash": hashlib.sha256(raw.encode()).hexdigest(),
        "raw_output_token_ids": token_ids,
        "assistant_token_mask": [True] * len(token_ids),
        "parsed_critic_output": dict(parsed) if parsed is not None else None,
        "parse_valid": parsed is not None,
        "finish_reason": "eos" if eos else "max_tokens",
        "truncated": not eos and len(token_ids) >= int(generation_config["max_new_tokens"]),
        "eos_present": eos,
        "token_count": len(token_ids),
        "excluded": empty,
        "exclusion_reason": "empty_raw_output" if empty else None,
        "model_revision": model_revision,
        "tokenizer_hash": tokenizer_hash,
        "generation_config": dict(generation_config),
        "generation_config_hash": stable_sha256(generation_config),
    }


def raw_record_valid(record: Mapping[str, Any]) -> bool:
    if record.get("excluded"):
        return False
    return bool(str(record.get("raw_critic_output") or "").strip() and record.get("raw_output_token_ids"))


def full_raw_teacher_forcing(
    *, prompt_ids: Sequence[int], raw_output_ids: Sequence[int], pad_token_id: int, multiple: int = 8
) -> dict[str, torch.Tensor]:
    if not prompt_ids or not raw_output_ids:
        raise ValueError("h1b_raw_trajectory_empty")
    prompt = torch.tensor(list(prompt_ids), dtype=torch.long).unsqueeze(0)
    assistant = torch.tensor(list(raw_output_ids), dtype=torch.long).unsqueeze(0)
    raw = torch.cat((prompt, assistant), dim=1)
    assistant_mask = torch.zeros_like(raw, dtype=torch.bool)
    assistant_mask[:, prompt.shape[1] :] = True
    attention = torch.ones_like(raw)
    padding = (-raw.shape[1]) % multiple
    if padding:
        pad = torch.full((1, padding), int(pad_token_id), dtype=torch.long)
        raw = torch.cat((raw, pad), dim=1)
        attention = torch.cat((attention, torch.zeros_like(pad)), dim=1)
        assistant_mask = torch.cat((assistant_mask, torch.zeros_like(pad, dtype=torch.bool)), dim=1)
    return {"full_ids": raw, "attention_mask": attention, "assistant_mask": assistant_mask}


def delta_regularization(module: nn.Module, source: torch.Tensor) -> torch.Tensor:
    if not isinstance(module, ZeroGatedResidualInner):
        return source.new_zeros(())
    return module.delta(source.float()).square().mean()


def token_group(
    token_id: int,
    decoded: str,
    *,
    special_ids: set[int],
    identifier_ids: set[int],
    structural_ids: set[int],
) -> str:
    if int(token_id) in special_ids:
        return "special"
    if int(token_id) in identifier_ids or re.search(r"(?:RULE|SOURCE|_R|_S|ID)[_-]?\d", decoded, re.I):
        return "identifier"
    stripped = decoded.strip()
    if int(token_id) in structural_ids or (stripped and all(not char.isalnum() for char in stripped)):
        return "structural"
    return "content"


def aggregate_group_retrieval(
    nearest: torch.Tensor,
    target_ids: torch.Tensor,
    groups: Sequence[str],
    seen_ids: set[int],
) -> dict[str, Any]:
    if len(groups) != target_ids.numel() or nearest.shape[0] != target_ids.numel():
        raise ValueError("h1b_group_metric_shape_invalid")
    targets = target_ids.detach().cpu().long()
    top1 = nearest[:, 0].eq(targets)
    top5 = nearest.eq(targets[:, None]).any(dim=1)
    labels = list(groups)
    output: dict[str, Any] = {}
    masks = {name: torch.tensor([value == name for value in labels]) for name in TOKEN_GROUPS}
    masks["seen"] = torch.tensor([int(value) in seen_ids for value in targets.tolist()])
    masks["unseen"] = ~masks["seen"]
    for name, mask in masks.items():
        output[name] = {
            "positions": int(mask.sum()),
            "top1": float(top1[mask].float().mean()) if mask.any() else None,
            "top5": float(top5[mask].float().mean()) if mask.any() else None,
        }
    output["overall"] = {
        "positions": int(targets.numel()),
        "top1": float(top1.float().mean()),
        "top5": float(top5.float().mean()),
        "unique_nearest_ratio": float(nearest[:, 0].unique().numel() / max(1, nearest.shape[0])),
    }
    return output


def validation_constraints(candidate: Mapping[str, Any], identity: Mapping[str, Any]) -> dict[str, Any]:
    cr, ir = candidate["retrieval"], identity["retrieval"]
    passed = bool(
        cr["overall"]["top5"] >= ir["overall"]["top5"] - 0.02
        and cr["content"]["top5"] >= ir["content"]["top5"]
        and cr["unseen"]["top5"] >= ir["unseen"]["top5"] - 0.03
        and float(candidate["mean_cosine_similarity"]) > float(identity["mean_cosine_similarity"])
        and float(candidate["mse"]) < float(identity["mse"])
        and not candidate.get("sequence_collapse")
        and not candidate.get("nan_or_inf")
    )
    return {
        "passed": passed,
        "classification": None if passed else "alignment_loss_improves_but_token_geometry_regresses",
    }


def selection_key(metrics: Mapping[str, Any]) -> tuple[float, ...]:
    retrieval = metrics["retrieval"]
    return (
        float(retrieval["content"]["top5"] or -1.0),
        float(retrieval["unseen"]["top5"] or -1.0),
        float(retrieval["overall"]["top5"]),
        float(metrics["mean_cosine_similarity"]),
        -float(metrics["mse"]),
        float(retrieval["overall"]["unique_nearest_ratio"]),
    )


def final_h1b_gate(candidate: Mapping[str, Any], identity: Mapping[str, Any], contract: Mapping[str, Any]) -> dict[str, Any]:
    cr, ir = candidate["retrieval"], identity["retrieval"]
    passed = bool(
        all(bool(contract.get(key)) for key in ("positionwise", "no_pooling", "shift", "assistant_mask", "eos", "padding"))
        and float(candidate["mean_cosine_similarity"]) > float(identity["mean_cosine_similarity"])
        and float(candidate["mse"]) < float(identity["mse"])
        and cr["overall"]["top5"] >= ir["overall"]["top5"] - 0.03
        and cr["content"]["top5"] >= ir["content"]["top5"] - 0.03
        and cr["unseen"]["top5"] >= ir["unseen"]["top5"] - 0.05
        and not candidate.get("sequence_collapse")
        and not candidate.get("nan_or_inf")
    )
    return {
        "passed": passed,
        "classification": "faithful_real_trajectory_inner_alignment_passed" if passed else (
            "alignment_loss_improves_but_token_geometry_regresses"
            if float(candidate["mean_cosine_similarity"]) > float(identity["mean_cosine_similarity"])
            else "real_trajectory_alignment_overfit"
        ),
    }


def identity_preferred(identity: Mapping[str, Any], candidates: Sequence[Mapping[str, Any]]) -> bool:
    return bool(candidates and all(selection_key(identity) >= selection_key(candidate) for candidate in candidates))


def architecture_manifest(module: nn.Module) -> dict[str, Any]:
    return {
        "class": type(module).__name__,
        "parameters": sum(parameter.numel() for parameter in module.parameters()),
        "dtype": sorted({str(parameter.dtype) for parameter in module.parameters()}),
        "state_shapes": {name: list(value.shape) for name, value in module.state_dict().items()},
        "sha256": stable_sha256({name: list(value.shape) for name, value in module.state_dict().items()}),
    }


def config_manifest(config: RealTrajectoryConfig | None = None) -> dict[str, Any]:
    value = config or RealTrajectoryConfig()
    value.validate()
    return {"profile_id": PROFILE_ID, "enabled": False, "config": asdict(value), "outer23_trained": False}
