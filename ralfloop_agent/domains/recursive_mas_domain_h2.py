from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
from pathlib import Path
from typing import Any, Mapping, Sequence

import torch
from torch import nn


PROFILE_ID = "recursive_mas_domain_h2_outer23_v1"
H1B_SHA256 = "7d48ad286a7b51138390e8845e2d41ddbf19dc6ae1d9daaae40f2cf579d42ad8"
H1B_FREEZE_SHA256 = "83a64cfe30e5b0cb41fc59805b71c64079cfa31ea1628e66fca1a5f9fbf3736c"
ALLOWED_CAPS = (16, 32)


@dataclass(frozen=True)
class H2Config:
    seed: int = 82031
    caps: tuple[int, int] = ALLOWED_CAPS
    micro_steps: int = 40
    max_steps: int = 300
    evaluate_every: int = 20
    checkpoint_every: int = 20
    gradient_accumulation: int = 4
    gradient_clip: float = 1.0
    learning_rate: float = 2e-4
    validation_patience: int = 3
    max_new_tokens: int = 512

    def validate(self) -> None:
        if self.caps != ALLOWED_CAPS:
            raise ValueError("h2_caps_invalid")
        if self.micro_steps != 40 or self.max_steps > 300:
            raise ValueError("h2_step_limit_invalid")
        if self.evaluate_every != 20 or self.checkpoint_every != 20:
            raise ValueError("h2_interval_invalid")
        if self.gradient_accumulation not in {4, 8}:
            raise ValueError("h2_accumulation_invalid")
        if self.max_new_tokens != 512:
            raise ValueError("h2_decode_limit_invalid")


class OrderedOuter23(nn.Module):
    """RecursiveMAS outer_ln_res_adapter, new H2 namespace only."""

    def __init__(self, in_dim: int = 1536, out_dim: int = 2048) -> None:
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
        delta = self.proj2(self.act(self.proj1(hidden)))
        return self.ln_target(delta + self.residual_proj(value))


def initialize_outer23(cap: int, *, seed: int = H2Config().seed) -> OrderedOuter23:
    if cap not in ALLOWED_CAPS:
        raise ValueError("h2_cap_invalid")
    torch.manual_seed(seed + cap)
    return OrderedOuter23().to(dtype=torch.float32)


def ordered_cap(value: torch.Tensor, cap: int) -> torch.Tensor:
    if value.ndim != 3 or value.shape[-1] != 1536:
        raise ValueError("h2_critic_trajectory_shape_invalid")
    if cap not in ALLOWED_CAPS:
        raise ValueError("h2_cap_invalid")
    if value.shape[1] < cap:
        raise ValueError("h2_trajectory_shorter_than_cap")
    return value[:, :cap, :]


def response_only_labels(prompt_tokens: int, latent_tokens: int, response_ids: torch.Tensor) -> torch.Tensor:
    if prompt_tokens < 1 or latent_tokens not in ALLOWED_CAPS or response_ids.ndim != 2:
        raise ValueError("h2_response_mask_shape_invalid")
    prefix = torch.full(
        (response_ids.shape[0], prompt_tokens + latent_tokens),
        -100,
        dtype=torch.long,
        device=response_ids.device,
    )
    return torch.cat((prefix, response_ids.long()), dim=1)


def layout_audit(
    *, prefix_tokens: int, latent_tokens: int, suffix_tokens: int, response_tokens: int
) -> dict[str, Any]:
    if min(prefix_tokens, suffix_tokens, response_tokens) < 1 or latent_tokens not in ALLOWED_CAPS:
        raise ValueError("h2_layout_invalid")
    prompt_tokens = prefix_tokens + latent_tokens + suffix_tokens
    total = prompt_tokens + response_tokens
    position_ids = list(range(total))
    return {
        "layout": ["prompt_prefix", "ordered_critic_latent", "prompt_suffix_evidence_packet", "assistant_response"],
        "prefix_tokens": prefix_tokens,
        "latent_tokens": latent_tokens,
        "suffix_tokens": suffix_tokens,
        "response_tokens": response_tokens,
        "inputs_embeds_length": total,
        "attention_mask_length": total,
        "position_ids_monotonic": all(right == left + 1 for left, right in zip(position_ids, position_ids[1:])),
        "generation_boundary": prompt_tokens,
        "solver_inner_called": False,
        "outer31_present": False,
        "pooling": None,
    }


def selection_key(metrics: Mapping[str, Any]) -> tuple[float, ...]:
    qualitative = sum(
        float(metrics.get(name) or 0.0)
        for name in ("contradiction_recall", "counterargument_coverage", "recommendation_condition_correctness")
    ) / 3.0
    errors = sum(int(metrics.get(name) or 0) for name in ("format_errors", "semantic_errors", "adapter_transfer_errors"))
    return (
        float(metrics.get("semantic_complete_count") or 0),
        float(metrics.get("schema_valid_count") or 0),
        qualitative,
        float(metrics.get("rule_recall") or 0),
        float(metrics.get("rule_precision") or 0),
        float(metrics.get("source_recall") or 0),
        float(metrics.get("source_precision") or 0),
        -float(errors),
        -float(metrics.get("wall_median_ms") or 0),
    )


def micro_gate(metrics: Mapping[str, Any]) -> dict[str, Any]:
    passed = bool(
        int(metrics.get("semantic_complete_count") or 0) >= 6
        and int(metrics.get("nan_or_inf") or 0) == 0
        and not metrics.get("oom")
        and float(metrics.get("outer23_gradient_norm") or 0) > 0
        and int(metrics.get("base_trainable_parameters") or 0) == 0
    )
    return {"passed": passed, "classification": "h2_micro_passed" if passed else "h2_micro_failed"}


def _qualitative_macro(metrics: Mapping[str, Any]) -> float:
    return sum(
        float(metrics.get(name) or 0.0)
        for name in ("contradiction_recall", "counterargument_coverage", "recommendation_condition_correctness")
    ) / 3.0


def h2_gate(candidate: Mapping[str, Any], baseline: Mapping[str, Any]) -> dict[str, Any]:
    candidate_macro = _qualitative_macro(candidate)
    baseline_macro = _qualitative_macro(baseline)
    relative = (candidate_macro - baseline_macro) / baseline_macro if baseline_macro > 0 else float(candidate_macro > 0)
    condition_1 = int(candidate.get("semantic_complete_count") or 0) >= int(baseline.get("semantic_complete_count") or 0) + 3
    condition_2 = relative >= 0.10
    constraints = bool(
        int(candidate.get("schema_valid_count") or 0) >= int(baseline.get("schema_valid_count") or 0)
        and float(candidate.get("rule_recall") or 0) >= float(baseline.get("rule_recall") or 0)
        and float(candidate.get("rule_precision") or 0) >= float(baseline.get("rule_precision") or 0)
        and float(candidate.get("source_recall") or 0) >= float(baseline.get("source_recall") or 0)
        and float(candidate.get("source_precision") or 0) >= float(baseline.get("source_precision") or 0)
        and int(candidate.get("invented_rule_ids") or 0) == 0
        and int(candidate.get("invented_source_ids") or 0) == 0
        and int(candidate.get("safety_violations") or 0) == 0
        and int(candidate.get("approval_violations") or 0) == 0
        and int(candidate.get("solver_format_regressions") or 0) == 0
    )
    passed = bool((condition_1 or condition_2) and constraints)
    return {
        "passed": passed,
        "condition_1_valid_count": condition_1,
        "condition_2_qualitative_relative": condition_2,
        "qualitative_relative_improvement": relative,
        "constraints_passed": constraints,
        "classification": "recursive_domain_h2_passed" if passed else "recursive_domain_h2_failed",
    }


def reserve_gate(candidate: Mapping[str, Any], baseline: Mapping[str, Any]) -> dict[str, Any]:
    candidate_macro = _qualitative_macro(candidate)
    baseline_macro = _qualitative_macro(baseline)
    relative = (candidate_macro - baseline_macro) / baseline_macro if baseline_macro > 0 else float(candidate_macro > 0)
    condition_1 = int(candidate.get("semantic_complete_count") or 0) >= int(baseline.get("semantic_complete_count") or 0) + 2
    condition_2 = relative >= 0.10
    constraints = bool(
        int(candidate.get("schema_valid_count") or 0) >= int(baseline.get("schema_valid_count") or 0)
        and all(
            float(candidate.get(name) or 0) >= float(baseline.get(name) or 0)
            for name in ("rule_recall", "rule_precision", "source_recall", "source_precision")
        )
        and int(candidate.get("invented_rule_ids") or 0) == 0
        and int(candidate.get("invented_source_ids") or 0) == 0
        and int(candidate.get("safety_violations") or 0) == 0
        and int(candidate.get("approval_violations") or 0) == 0
    )
    passed = bool((condition_1 or condition_2) and constraints)
    return {
        "passed": passed,
        "condition_1_valid_count": condition_1,
        "condition_2_qualitative_relative": condition_2,
        "qualitative_relative_improvement": relative,
        "constraints_passed": constraints,
        "classification": (
            "recursive_qwen3_domain_candidate_for_integration" if passed else "recursive_domain_reserve_b_failed"
        ),
    }


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def stable_sha256(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def architecture_manifest(module: nn.Module) -> dict[str, Any]:
    state = module.state_dict()
    return {
        "class": type(module).__name__,
        "adapter_type": "outer_ln_res_adapter",
        "dimensions": [1536, 2048],
        "parameters": sum(parameter.numel() for parameter in module.parameters()),
        "trainable_parameters": sum(parameter.numel() for parameter in module.parameters() if parameter.requires_grad),
        "dtype": sorted({str(value.dtype) for value in state.values()}),
        "state_shapes": {name: list(value.shape) for name, value in state.items()},
        "architecture_sha256": stable_sha256({name: list(value.shape) for name, value in state.items()}),
    }


def lab_manifest(config: H2Config | None = None) -> dict[str, Any]:
    value = config or H2Config()
    value.validate()
    return {
        "profile_id": PROFILE_ID,
        "enabled": False,
        "registered": False,
        "config": asdict(value),
        "trainable_components": ["new_outer23"],
        "critic_inner_frozen": True,
        "solver_inner_bypassed": True,
        "outer31_present": False,
        "pooling": None,
    }
