from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
from pathlib import Path
from typing import Any, Iterable, Mapping

import torch
from torch import nn

from .recursive_mas_domain_instruct_training import (
    NativeCrossModelAdapter,
    NativeInnerAdapter,
    gradient_norm,
)


PROFILE_ID = "recursive_mas_domain_qwen3_solver_v1"
ARTIFACT_ROOT = Path(".ralf_run/recursive_domain_qwen3_micro_overfit")
FIXTURE_HASH = "1b38b1fd9d1d64eef98eeecede0dde8ce614f9ca4bd025594d476d6eff6795eb"
MODEL_SPECS = {
    "planner": {
        "model_id": "Qwen/Qwen2.5-3B-Instruct",
        "revision": "aa8e72537993ba99e69dfaafa59ed015b17504d1",
        "hidden_size": 2048,
    },
    "critic": {
        "model_id": "Qwen/Qwen2.5-1.5B-Instruct",
        "revision": "989aa7980e4cf806f80c7fef2b1adb7bc71aa306",
        "hidden_size": 1536,
    },
    "solver": {
        "model_id": "Qwen/Qwen3-1.7B",
        "revision": "70d244cc86ccca08cf5af4e1e306ecf908b1ad5e",
        "hidden_size": 2048,
    },
}
TRAINED_ADAPTERS = ("planner_inner", "outer12", "critic_inner", "outer23", "solver_inner")
STAGE_A_ADAPTERS = ("critic_inner", "outer23", "solver_inner")


@dataclass(frozen=True)
class NativeMicroConfig:
    seed: int = 42
    max_steps: int = 120
    checkpoint_every: int = 10
    evaluate_every: int = 10
    batch_size: int = 1
    gradient_accumulation: int = 4
    target_sequence_lengths: tuple[int, ...] = (128, 256, 384)
    max_target_sequence_length: int = 512
    mixed_precision: str = "float16"
    learning_rate: float = 2e-4
    gradient_clip: float = 1.0
    final_token_ce_weight: float = 4.0
    planner_structured_weight: float = 1.0
    critic_structured_weight: float = 1.0
    latent_regularization_weight: float = 0.25
    early_stopping_patience: int = 3

    def validate(self) -> None:
        if not 1 <= self.max_steps <= 120:
            raise ValueError("qwen3_micro_step_limit_invalid")
        if self.checkpoint_every != 10 or self.evaluate_every != 10:
            raise ValueError("qwen3_micro_interval_invalid")
        if self.batch_size != 1 or self.gradient_accumulation < 1:
            raise ValueError("qwen3_micro_memory_strategy_invalid")
        if self.target_sequence_lengths != (128, 256, 384) or self.max_target_sequence_length > 512:
            raise ValueError("qwen3_micro_sequence_probe_invalid")
        if self.final_token_ce_weight <= max(
            self.planner_structured_weight,
            self.critic_structured_weight,
            self.latent_regularization_weight,
        ):
            raise ValueError("qwen3_final_token_ce_not_dominant")


def native_graph() -> list[dict[str, Any]]:
    return [
        {
            "tensor_source": "planner_hidden",
            "adapter": "planner_inner",
            "shape": ["batch", "sequence", 2048],
            "tensor_destination": "planner_latent",
            "used_by_final_decode": True,
            "final_ce_gradient_when_cached": False,
            "surrogate_loss": "planner_structured_loss",
        },
        {
            "tensor_source": "planner_latent",
            "adapter": "outer12",
            "shape": [2048, 1536],
            "tensor_destination": "critic_inputs_embeds",
            "used_by_final_decode": True,
            "final_ce_gradient_when_cached": False,
            "surrogate_loss": "critic_hidden_alignment",
        },
        {
            "tensor_source": "critic_hidden",
            "adapter": "critic_inner",
            "shape": ["batch", "sequence", 1536],
            "tensor_destination": "critic_latent",
            "used_by_final_decode": True,
            "final_ce_gradient_when_cached": True,
            "loss": "solver_final_token_CE",
        },
        {
            "tensor_source": "critic_latent",
            "adapter": "outer23",
            "shape": [1536, 2048],
            "tensor_destination": "solver_hidden",
            "used_by_final_decode": True,
            "final_ce_gradient_when_cached": True,
            "loss": "solver_final_token_CE",
        },
        {
            "tensor_source": "solver_hidden",
            "adapter": "solver_inner",
            "shape": ["batch", "sequence", 2048],
            "tensor_destination": "Qwen3_inputs_embeds",
            "used_by_final_decode": True,
            "final_ce_gradient_when_cached": True,
            "loss": "solver_final_token_CE",
        },
        {
            "tensor_source": "Qwen3_inputs_embeds",
            "adapter": "Qwen3_frozen_solver",
            "tensor_destination": "frozen_solver_logits",
            "used_by_final_decode": True,
            "loss": "response_only_final_token_CE",
        },
        {
            "tensor_source": "solver_hidden",
            "adapter": "outer31",
            "shape": [2048, 2048],
            "tensor_destination": "next_round_planner_hidden",
            "used_by_final_decode": False,
            "trained": False,
            "reason": "solver_to_planner_next_round_only",
        },
        {
            "tensor_source": "frozen_solver_logits",
            "adapter": None,
            "tensor_destination": "strict_parser_then_domain_opinion_v1",
            "used_by_final_decode": True,
        },
    ]


def initialize_native_adapters(seed: int = 42) -> nn.ModuleDict:
    torch.manual_seed(seed)
    return nn.ModuleDict(
        {
            "planner_inner": NativeInnerAdapter(2048),
            "outer12": NativeCrossModelAdapter(2048, 1536),
            "critic_inner": NativeInnerAdapter(1536),
            "outer23": NativeCrossModelAdapter(1536, 2048),
            "solver_inner": NativeInnerAdapter(2048),
        }
    )


def adapter_manifest(adapters: Mapping[str, nn.Module]) -> dict[str, Any]:
    if tuple(adapters.keys()) != TRAINED_ADAPTERS:
        raise ValueError("qwen3_micro_adapter_namespace_invalid")
    return {
        name: {
            "parameters": sum(parameter.numel() for parameter in module.parameters()),
            "trainable_parameters": sum(parameter.numel() for parameter in module.parameters() if parameter.requires_grad),
            "state_shapes": {key: list(value.shape) for key, value in module.state_dict().items()},
        }
        for name, module in adapters.items()
    }


def tensor_sha256(tensor: torch.Tensor) -> str:
    value = tensor.detach().cpu().contiguous()
    digest = hashlib.sha256()
    digest.update(str(value.dtype).encode())
    digest.update(json.dumps(list(value.shape), separators=(",", ":")).encode())
    digest.update(value.view(torch.uint8).numpy().tobytes())
    return digest.hexdigest()


def save_hidden_cache(
    path: Path,
    *,
    tensor: torch.Tensor,
    attention_mask: torch.Tensor,
    metadata: Mapping[str, Any],
) -> dict[str, Any]:
    if tensor.ndim != 3 or attention_mask.ndim != 2 or tensor.shape[:2] != attention_mask.shape:
        raise ValueError("hidden_cache_attention_mask_shape_invalid")
    record = {
        **dict(metadata),
        "shape": list(tensor.shape),
        "dtype": str(tensor.dtype),
        "device": "cpu",
        "attention_mask_shape": list(attention_mask.shape),
        "attention_mask_sha256": tensor_sha256(attention_mask),
        "tensor_sha256": tensor_sha256(tensor),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    torch.save(
        {"tensor": tensor.detach().cpu(), "attention_mask": attention_mask.detach().cpu()},
        path,
    )
    path.with_suffix(".json").write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return record


def load_hidden_cache(path: Path, *, expected: Mapping[str, Any]) -> tuple[torch.Tensor, torch.Tensor, dict[str, Any]]:
    metadata = json.loads(path.with_suffix(".json").read_text(encoding="utf-8"))
    for key, value in expected.items():
        if metadata.get(key) != value:
            raise RuntimeError(f"hidden_cache_metadata_mismatch:{key}")
    payload = torch.load(path, map_location="cpu", weights_only=True)
    tensor = payload["tensor"]
    mask = payload["attention_mask"]
    if tensor_sha256(tensor) != metadata["tensor_sha256"]:
        raise RuntimeError("hidden_cache_tensor_hash_mismatch")
    if tensor_sha256(mask) != metadata["attention_mask_sha256"]:
        raise RuntimeError("hidden_cache_attention_mask_hash_mismatch")
    return tensor, mask, metadata


def response_only_loss_labels(
    prompt_length: int,
    response_ids: torch.Tensor,
    response_attention_mask: torch.Tensor,
) -> torch.Tensor:
    if response_ids.ndim != 2 or response_attention_mask.shape != response_ids.shape or prompt_length < 1:
        raise ValueError("response_only_mask_shape_invalid")
    labels = response_ids.to(dtype=torch.long).clone()
    labels[response_attention_mask == 0] = -100
    prefix = torch.full(
        (response_ids.shape[0], prompt_length),
        -100,
        dtype=torch.long,
        device=response_ids.device,
    )
    return torch.cat((prefix, labels), dim=1)


def freeze_base_model(model: nn.Module) -> dict[str, Any]:
    model.eval()
    for parameter in model.parameters():
        parameter.requires_grad = False
    return {
        "parameters": sum(parameter.numel() for parameter in model.parameters()),
        "trainable_parameters": sum(parameter.numel() for parameter in model.parameters() if parameter.requires_grad),
        "frozen": all(not parameter.requires_grad for parameter in model.parameters()),
    }


def adapter_gradient_report(adapters: Mapping[str, nn.Module], *, detached_upstream: bool) -> dict[str, Any]:
    norms = {name: gradient_norm(module) for name, module in adapters.items()}
    return {
        "norms": norms,
        "detached_upstream": detached_upstream,
        "final_ce_reaches": {
            "planner_inner": not detached_upstream and norms.get("planner_inner", 0.0) > 0,
            "outer12": not detached_upstream and norms.get("outer12", 0.0) > 0,
            "critic_inner": norms.get("critic_inner", 0.0) > 0,
            "outer23": norms.get("outer23", 0.0) > 0,
            "solver_inner": norms.get("solver_inner", 0.0) > 0,
        },
        "upstream_training_claim": "structured_surrogate_only" if detached_upstream else "end_to_end_final_CE",
        "outer31_present": False,
    }


def _common_gate(metrics: Mapping[str, Any], *, schema_required: int) -> bool:
    return bool(
        int(metrics.get("valid_count") or 0) >= 7
        and int(metrics.get("schema_valid_count") or 0) >= schema_required
        and float(metrics.get("rule_accuracy") or 0) >= 0.875
        and float(metrics.get("source_accuracy") or 0) >= 0.875
        and int(metrics.get("invented_ids") or 0) == 0
    )


def stage_a_gate(metrics: Mapping[str, Any]) -> dict[str, Any]:
    passed = _common_gate(metrics, schema_required=8) and bool(
        float(metrics.get("contradiction_inclusion") or 0) == 1.0
        and float(metrics.get("counterargument_coverage") or 0) == 1.0
    )
    return {"passed": passed, "classification": "stage_a_passed" if passed else "critic_solver_bridge_not_trainable"}


def stage_b_gate(metrics: Mapping[str, Any]) -> dict[str, Any]:
    passed = _common_gate(metrics, schema_required=7)
    return {"passed": passed, "classification": "stage_b_passed" if passed else "native_pipeline_semantic_collapse"}


def stage_c_gate(metrics: Mapping[str, Any]) -> dict[str, Any]:
    passed = _common_gate(metrics, schema_required=8) and bool(
        float(metrics.get("valid_packet_contradiction_inclusion") or 0) == 1.0
        and float(metrics.get("valid_packet_counterargument_coverage") or 0) == 1.0
        and float(metrics.get("uncertainty_presence") or 0) == 1.0
        and float(metrics.get("recommendation_presence") or 0) == 1.0
        and int(metrics.get("demo_contamination") or 0) == 0
        and int(metrics.get("safety_violations") or 0) == 0
        and int(metrics.get("approval_violations") or 0) == 0
    )
    return {
        "passed": passed,
        "classification": "native_qwen3_recursive_micro_overfit_passed" if passed else "native_pipeline_semantic_collapse",
    }


def save_adapter_checkpoint(
    module: nn.Module,
    path: Path,
    *,
    metadata: Mapping[str, Any],
) -> dict[str, Any]:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "state_dict": {name: value.detach().cpu() for name, value in module.state_dict().items()},
        "metadata": dict(metadata),
    }
    torch.save(payload, path)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    record = {
        **dict(metadata),
        "path": str(path),
        "sha256": digest,
        "parameters": sum(parameter.numel() for parameter in module.parameters()),
        "state_shapes": {key: list(value.shape) for key, value in module.state_dict().items()},
        "dtype": sorted({str(value.dtype) for value in module.state_dict().values()}),
    }
    path.with_suffix(".json").write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return record


def load_adapter_checkpoint(module: nn.Module, path: Path, *, expected_sha256: str) -> dict[str, Any]:
    if hashlib.sha256(path.read_bytes()).hexdigest() != expected_sha256:
        raise RuntimeError("adapter_checkpoint_hash_mismatch")
    payload = torch.load(path, map_location="cpu", weights_only=True)
    module.load_state_dict(payload["state_dict"], strict=True)
    return dict(payload["metadata"])


def micro_manifest(
    *,
    config: NativeMicroConfig,
    tokenizer_hashes: Mapping[str, str],
    adapter_shapes: Mapping[str, Any],
    dataset_hash: str,
    evidence_packet_hash: str,
) -> dict[str, Any]:
    config.validate()
    return {
        "profile_id": PROFILE_ID,
        "models": MODEL_SPECS,
        "tokenizer_hashes": dict(tokenizer_hashes),
        "adapter_shapes": dict(adapter_shapes),
        "seed": config.seed,
        "dataset_hash": dataset_hash,
        "fixture_hash": FIXTURE_HASH,
        "evidence_packet_hash": evidence_packet_hash,
        "training_config": asdict(config),
        "checkpoint_hashes": {},
        "outer31_created": False,
    }
