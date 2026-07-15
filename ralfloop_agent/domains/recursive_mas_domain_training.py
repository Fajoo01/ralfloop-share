from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
import hashlib
import json
import os
from pathlib import Path
import resource
import shutil
import sys
import time
from typing import Any, Callable, Iterable

from .recursive_mas_domain_dataset import deterministic_splits, generate_dataset
from .recursive_mas_domain_prompts import (
    PLANNER_SLOT,
    REFINED_SLOT,
    build_domain_critic_prompt_with_slot,
    build_domain_planner_prompt,
    build_domain_solver_prompt_with_slots,
)
from .recursive_mas_profiles import (
    DOMAIN_TRAINING_ROOT,
    MATH_PROFILE,
    MATH_SNAPSHOTS,
    verify_math_checkpoint_hashes,
)


UPSTREAM_ROOT = Path("/home/sibilla-cumana/RecursiveMAS")
REPO_ROOT = Path("/home/sibilla-cumana/ralfloop_agent_scaffold")
HIDDEN_SIZES = {"planner": 2048, "critic": 2048, "solver": 1536}
OUTER_DIMS = {
    "outer_12": (2048, 2048),
    "outer_23": (2048, 1536),
    "outer_31": (1536, 2048),
}
ROLE_TO_BRIDGE = {"planner": "outer_31", "critic": "outer_12", "solver": "outer_23"}
LINK_SOURCE_ROLE = {"outer_12": "planner", "outer_23": "critic", "outer_31": "solver"}
LINK_TARGET_ROLE = {"outer_12": "critic", "outer_23": "solver", "outer_31": "planner"}


@dataclass(frozen=True)
class DomainTrainingConfig:
    seed: int = 42
    dtype: str = "float16"
    max_length: int = 1024
    max_feature_tokens: int = 96
    gate_train_cases: int = 2
    validation_cases: int = 4
    pilot_train_cases: int = 24
    dry_run_steps: int = 1
    overfit_steps: int = 10
    pilot_steps_per_component: int = 25
    gradient_accumulation: int = 4
    learning_rate: float = 2e-4
    checkpoint_every: int = 25
    early_stopping_patience: int = 8
    max_pilot_steps: int = 200
    timeout_seconds: int = 3600

    @property
    def pilot_total_steps(self) -> int:
        return self.pilot_steps_per_component * 6

    def validate(self) -> None:
        if self.pilot_total_steps > self.max_pilot_steps:
            raise ValueError("pilot_step_limit_exceeded")
        if self.max_length < 128 or self.max_feature_tokens < 1:
            raise ValueError("training_sequence_configuration_invalid")
        if self.gradient_accumulation < 1:
            raise ValueError("gradient_accumulation_invalid")


@dataclass
class FeatureExample:
    case_id: str
    source: Any
    target: Any
    bridge_target: Any


def adapter_objective(prediction: Any, target: Any) -> Any:
    import torch.nn.functional as functional

    pred = prediction.float()
    expected = target.float()
    cosine = 1.0 - functional.cosine_similarity(pred, expected, dim=-1).mean()
    mse = functional.mse_loss(pred, expected)
    return cosine + 0.05 * mse


def train_component(
    module: Any,
    examples: list[tuple[Any, Any]],
    *,
    steps: int,
    learning_rate: float,
    gradient_accumulation: int,
    device: str,
    patience: int | None = None,
) -> dict[str, Any]:
    import torch

    if not examples:
        raise ValueError("training_examples_required")
    module.to(device=device, dtype=torch.float32)
    module.train()
    optimizer = torch.optim.AdamW(module.parameters(), lr=learning_rate, betas=(0.9, 0.95))
    losses: list[float] = []
    best = float("inf")
    stale = 0
    for step in range(steps):
        optimizer.zero_grad(set_to_none=True)
        accumulated = 0.0
        for offset in range(gradient_accumulation):
            source, target = examples[(step * gradient_accumulation + offset) % len(examples)]
            source = source.to(device=device, dtype=torch.float32)
            target = target.to(device=device, dtype=torch.float32)
            loss = adapter_objective(module(source), target)
            (loss / gradient_accumulation).backward()
            accumulated += float(loss.detach().cpu())
        torch.nn.utils.clip_grad_norm_(module.parameters(), 1.0)
        optimizer.step()
        current = accumulated / gradient_accumulation
        losses.append(current)
        if current < best - 1e-7:
            best = current
            stale = 0
        else:
            stale += 1
        if patience is not None and stale >= patience:
            break
    module.eval()
    return {
        "steps": len(losses),
        "losses": losses,
        "loss_initial": losses[0],
        "loss_final": losses[-1],
        "loss_min": min(losses),
        "loss_decreased": losses[-1] < losses[0],
        "early_stopped": len(losses) < steps,
    }


def evaluate_component(module: Any, examples: list[tuple[Any, Any]], device: str) -> float:
    import torch

    if not examples:
        raise ValueError("validation_examples_required")
    module.to(device=device, dtype=torch.float32)
    module.eval()
    with torch.inference_mode():
        values = [
            float(adapter_objective(module(source.to(device=device, dtype=torch.float32)), target.to(device=device, dtype=torch.float32)).cpu())
            for source, target in examples
        ]
    return sum(values) / len(values)


def save_component(module: Any, path: Path, metadata: dict[str, Any]) -> str:
    import torch

    path.parent.mkdir(parents=True, exist_ok=True)
    state = {name: value.detach().cpu() for name, value in module.state_dict().items()}
    torch.save(state, path)
    path.with_suffix(".json").write_text(json.dumps(metadata, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return _sha256(path)


def load_component(module: Any, path: Path) -> Any:
    import torch

    state = torch.load(path, map_location="cpu", weights_only=True)
    module.load_state_dict(state, strict=True)
    return module


def shape_compatibility() -> dict[str, Any]:
    import torch

    _prepare_upstream_imports()
    from modeling import Adapter, CrossModelAdapter  # type: ignore

    components: dict[str, Any] = {}
    for role, hidden in HIDDEN_SIZES.items():
        module = Adapter(hidden, "ln_res_adapter")
        load_component(module, Path(MATH_PROFILE.inner_adapter_checkpoints[role]))
        sample = torch.zeros((1, 2, hidden), dtype=torch.float32)
        components[role] = {"input": list(sample.shape), "output": list(module(sample).shape), "ok": list(module(sample).shape) == list(sample.shape)}
        del module, sample
    for name, (source, target) in OUTER_DIMS.items():
        module = CrossModelAdapter(source, target, "outer_ln_res_adapter")
        load_component(module, Path(MATH_PROFILE.outer_adapter_checkpoints[name]))
        sample = torch.zeros((1, 2, source), dtype=torch.float32)
        output = module(sample)
        components[name] = {"input": list(sample.shape), "output": list(output.shape), "ok": list(output.shape) == [1, 2, target]}
        del module, sample, output
    return {"ok": all(item["ok"] for item in components.values()), "components": components}


def run_training_lab(config: DomainTrainingConfig, artifact_root: Path = DOMAIN_TRAINING_ROOT) -> dict[str, Any]:
    import torch

    config.validate()
    started = time.monotonic()
    _require_safe_runtime(torch)
    math_hashes = verify_math_checkpoint_hashes()
    if not math_hashes["ok"]:
        raise RuntimeError("math_checkpoint_hash_mismatch")
    prompt_canary = artifact_root / "canaries/prompt_only/manifest.json"
    if not prompt_canary.is_file():
        raise RuntimeError("prompt_only_canary_missing")
    prompt_result = json.loads(prompt_canary.read_text(encoding="utf-8"))
    if prompt_result.get("result") != "prompt_only_insufficient":
        raise RuntimeError("prompt_only_training_gate_not_met")

    cases, traces = generate_dataset()
    case_by_id = {case["id"]: case for case in cases}
    trace_by_id = {trace["case_id"]: trace for trace in traces}
    splits = deterministic_splits(cases)
    selected_train = splits["train"][: config.pilot_train_cases]
    selected_validation = splits["validation"][: config.validation_cases]
    selected_ids = selected_train + selected_validation
    _prepare_artifact_tree(artifact_root, config)
    shape = shape_compatibility()
    if not shape["ok"]:
        raise RuntimeError("adapter_shape_incompatible")

    torch.manual_seed(config.seed)
    torch.cuda.reset_peak_memory_stats()
    features: dict[str, dict[str, FeatureExample]] = {}
    for role in ("planner", "critic", "solver"):
        _check_timeout(started, config.timeout_seconds)
        role_features = extract_role_features(
            role,
            [case_by_id[item] for item in selected_ids],
            trace_by_id,
            config,
        )
        features[role] = {item.case_id: item for item in role_features}
        feature_path = artifact_root / f"checkpoints/features/{role}.pt"
        feature_path.parent.mkdir(parents=True, exist_ok=True)
        torch.save(
            {
                case_id: {"source": item.source, "target": item.target, "bridge_target": item.bridge_target}
                for case_id, item in features[role].items()
            },
            feature_path,
        )

    inner_results: dict[str, Any] = {}
    final_inner: dict[str, Any] = {}
    for role in ("planner", "critic", "solver"):
        _check_timeout(started, config.timeout_seconds)
        train_examples = [(features[role][item].source, features[role][item].target) for item in selected_train]
        validation_examples = [(features[role][item].source, features[role][item].target) for item in selected_validation]
        dry_module = _new_inner(role)
        dry = train_component(dry_module, train_examples[:1], steps=1, learning_rate=config.learning_rate, gradient_accumulation=1, device="cuda:0")
        save_component(dry_module, artifact_root / f"checkpoints/dry_run/{role}.pt", {"gate": "one_step", **dry})
        del dry_module

        overfit_module = _new_inner(role)
        overfit = train_component(overfit_module, train_examples[:2], steps=config.overfit_steps, learning_rate=config.learning_rate, gradient_accumulation=config.gradient_accumulation, device="cuda:0")
        overfit_validation = evaluate_component(overfit_module, validation_examples, "cuda:0")
        save_component(overfit_module, artifact_root / f"checkpoints/overfit/{role}.pt", {"gate": "overfit", "validation_loss": overfit_validation, **overfit})
        del overfit_module
        if not overfit["loss_decreased"]:
            raise RuntimeError(f"overfit_loss_not_reduced:{role}")

        pilot_module = _new_inner(role)
        initial_validation = evaluate_component(pilot_module, validation_examples, "cuda:0")
        pilot = train_component(
            pilot_module,
            train_examples,
            steps=config.pilot_steps_per_component,
            learning_rate=config.learning_rate,
            gradient_accumulation=config.gradient_accumulation,
            device="cuda:0",
            patience=config.early_stopping_patience,
        )
        final_validation = evaluate_component(pilot_module, validation_examples, "cuda:0")
        final_path = artifact_root / f"final/{role}/adapter.pt"
        digest = save_component(pilot_module, final_path, {"adapter_type": "ln_res_adapter", "role": role, "steps": pilot["steps"]})
        save_component(pilot_module, artifact_root / f"checkpoints/checkpoint-{pilot['steps']:03d}/{role}.pt", {"role": role, "steps": pilot["steps"]})
        pilot_module.cpu()
        final_inner[role] = pilot_module
        inner_results[role] = {
            "dry_run": dry,
            "overfit": overfit,
            "overfit_validation_loss": overfit_validation,
            "pilot": pilot,
            "validation_loss_initial": initial_validation,
            "validation_loss_final": final_validation,
            "checkpoint": str(final_path),
            "sha256": digest,
        }
        torch.cuda.empty_cache()

    outer_results: dict[str, Any] = {}
    for name in ("outer_12", "outer_23", "outer_31"):
        _check_timeout(started, config.timeout_seconds)
        source_role = LINK_SOURCE_ROLE[name]
        target_role = LINK_TARGET_ROLE[name]
        inner = final_inner[source_role].to(device="cuda:0", dtype=torch.float32).eval()
        outer_train = []
        outer_validation = []
        with torch.inference_mode():
            for case_id in selected_train:
                source = features[source_role][case_id].source.to("cuda:0", dtype=torch.float32)
                translated = inner(source).mean(dim=1, keepdim=True).cpu()
                target = features[target_role][case_id].bridge_target.mean(dim=1, keepdim=True)
                outer_train.append((translated, target))
            for case_id in selected_validation:
                source = features[source_role][case_id].source.to("cuda:0", dtype=torch.float32)
                translated = inner(source).mean(dim=1, keepdim=True).cpu()
                target = features[target_role][case_id].bridge_target.mean(dim=1, keepdim=True)
                outer_validation.append((translated, target))
        inner.cpu()
        torch.cuda.empty_cache()

        dry_module = _new_outer(name)
        dry = train_component(dry_module, outer_train[:1], steps=1, learning_rate=config.learning_rate, gradient_accumulation=1, device="cuda:0")
        save_component(dry_module, artifact_root / f"checkpoints/dry_run/{name}.pt", {"gate": "one_step", **dry})
        del dry_module
        overfit_module = _new_outer(name)
        overfit = train_component(overfit_module, outer_train[:2], steps=config.overfit_steps, learning_rate=config.learning_rate, gradient_accumulation=config.gradient_accumulation, device="cuda:0")
        overfit_validation = evaluate_component(overfit_module, outer_validation, "cuda:0")
        save_component(overfit_module, artifact_root / f"checkpoints/overfit/{name}.pt", {"gate": "overfit", "validation_loss": overfit_validation, **overfit})
        del overfit_module
        if not overfit["loss_decreased"]:
            raise RuntimeError(f"overfit_loss_not_reduced:{name}")

        pilot_module = _new_outer(name)
        initial_validation = evaluate_component(pilot_module, outer_validation, "cuda:0")
        pilot = train_component(
            pilot_module,
            outer_train,
            steps=config.pilot_steps_per_component,
            learning_rate=config.learning_rate,
            gradient_accumulation=config.gradient_accumulation,
            device="cuda:0",
            patience=config.early_stopping_patience,
        )
        final_validation = evaluate_component(pilot_module, outer_validation, "cuda:0")
        final_path = artifact_root / f"final/outer/{name}.pt"
        digest = save_component(pilot_module, final_path, {"adapter_type": "outer_ln_res_adapter", "link": name, "steps": pilot["steps"]})
        save_component(pilot_module, artifact_root / f"checkpoints/checkpoint-{pilot['steps']:03d}/{name}.pt", {"link": name, "steps": pilot["steps"]})
        pilot_module.cpu()
        outer_results[name] = {
            "dry_run": dry,
            "overfit": overfit,
            "overfit_validation_loss": overfit_validation,
            "pilot": pilot,
            "validation_loss_initial": initial_validation,
            "validation_loss_final": final_validation,
            "checkpoint": str(final_path),
            "sha256": digest,
        }
        torch.cuda.empty_cache()

    result = {
        "ok": True,
        "profile_id": "recursive_mas_domain_reasoning",
        "prompt_only_result": prompt_result["result"],
        "shape_compatibility": shape,
        "inner": inner_results,
        "outer": outer_results,
        "base_models_frozen": True,
        "gpu_stagewise_feature_extraction": True,
        "simultaneous_base_models": 1,
        "pilot_steps": sum(item["pilot"]["steps"] for item in [*inner_results.values(), *outer_results.values()]),
        "configured_pilot_step_limit": config.max_pilot_steps,
        "gpu_peak_bytes": int(torch.cuda.max_memory_allocated()),
        "ram_peak_bytes": int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024),
        "duration_ms": (time.monotonic() - started) * 1000.0,
        "feature_enabled": False,
    }
    manifest = _write_manifest(artifact_root, config, result)
    result["manifest"] = manifest
    (artifact_root / "metrics/training_result.json").write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return result


def extract_role_features(
    role: str,
    cases: list[dict[str, Any]],
    trace_by_id: dict[str, dict[str, Any]],
    config: DomainTrainingConfig,
) -> list[FeatureExample]:
    import torch
    from transformers import AutoModelForCausalLM, AutoTokenizer

    model_path = Path(getattr(MATH_PROFILE, f"{role}_checkpoint")).parent
    tokenizer = AutoTokenizer.from_pretrained(model_path, local_files_only=True, trust_remote_code=True, use_fast=True)
    if tokenizer.pad_token_id is None:
        tokenizer.pad_token = tokenizer.eos_token
    model = AutoModelForCausalLM.from_pretrained(
        model_path,
        dtype=torch.float16,
        local_files_only=True,
        trust_remote_code=True,
    )
    for parameter in model.parameters():
        parameter.requires_grad = False
    model.eval().to("cuda:0")
    embedding = model.get_input_embeddings()
    output: list[FeatureExample] = []
    try:
        with torch.inference_mode():
            for case in cases:
                trace = trace_by_id[case["id"]]
                prompt, assistant, bridge_text = _role_texts(role, case, trace)
                prompt_ids = _chat_ids(tokenizer, prompt, None)
                full_ids = _chat_ids(tokenizer, prompt, assistant)
                prompt_len = min(len(prompt_ids), len(full_ids))
                if len(full_ids) > config.max_length:
                    offset = len(full_ids) - config.max_length
                    full_ids = full_ids[offset:]
                    prompt_len = max(0, prompt_len - offset)
                if prompt_len < 1 or prompt_len >= len(full_ids):
                    raise RuntimeError(f"assistant_span_missing:{role}:{case['id']}")
                ids = torch.tensor(full_ids, dtype=torch.long, device="cuda:0").unsqueeze(0)
                attention = torch.ones_like(ids)
                model_output = model(input_ids=ids, attention_mask=attention, output_hidden_states=True, use_cache=False, return_dict=True)
                hidden = model_output.hidden_states[-1][:, prompt_len - 1 : -1, :]
                targets = embedding(ids)[:, prompt_len:, :]
                hidden, targets = _sample_pair(hidden, targets, config.max_feature_tokens)
                bridge_ids = tokenizer(bridge_text, add_special_tokens=False, truncation=True, max_length=config.max_feature_tokens)["input_ids"]
                bridge_tensor = torch.tensor(bridge_ids, dtype=torch.long, device="cuda:0").unsqueeze(0)
                bridge = embedding(bridge_tensor)
                output.append(
                    FeatureExample(
                        case_id=case["id"],
                        source=hidden.detach().cpu().float(),
                        target=targets.detach().cpu().float(),
                        bridge_target=bridge.detach().cpu().float(),
                    )
                )
    finally:
        model.to("cpu")
        del model, embedding
        torch.cuda.empty_cache()
    return output


def _role_texts(role: str, case: dict[str, Any], trace: dict[str, Any]) -> tuple[str, str, str]:
    core = {
        key: case[key]
        for key in (
            "domain_id",
            "domain_version",
            "question",
            "facts",
            "rules",
            "sources",
            "constraints",
            "known_contradictions",
            "reason_codes",
        )
    }
    question = json.dumps(core, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    planner_text = json.dumps(trace["planner_target"], ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    critic_text = json.dumps(trace["critic_target"], ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    solver_text = json.dumps(trace["domain_opinion_target"], ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    if role == "planner":
        return build_domain_planner_prompt(question), planner_text, solver_text
    if role == "critic":
        return build_domain_critic_prompt_with_slot(question).replace(PLANNER_SLOT, planner_text), critic_text, planner_text
    if role == "solver":
        return build_domain_solver_prompt_with_slots(question).replace(REFINED_SLOT, critic_text), solver_text, critic_text
    raise ValueError(f"unsupported_role:{role}")


def _chat_ids(tokenizer: Any, prompt: str, assistant: str | None) -> list[int]:
    messages = [
        {"role": "system", "content": "You are a careful domain-reasoning assistant."},
        {"role": "user", "content": prompt},
    ]
    if assistant is not None:
        messages.append({"role": "assistant", "content": assistant})
    kwargs = {"tokenize": True, "add_generation_prompt": assistant is None, "enable_thinking": False}
    try:
        value = tokenizer.apply_chat_template(messages, **kwargs)
    except TypeError:
        kwargs.pop("enable_thinking", None)
        value = tokenizer.apply_chat_template(messages, **kwargs)
    except Exception as exc:
        if "roles must alternate" not in str(exc) or len(messages) < 2:
            raise
        merged = [{"role": "user", "content": messages[0]["content"] + "\n\n" + messages[1]["content"]}, *messages[2:]]
        value = tokenizer.apply_chat_template(merged, **kwargs)
    if hasattr(value, "tolist"):
        value = value.tolist()
    if value and isinstance(value[0], list):
        value = value[0]
    return [int(item) for item in value]


def _sample_pair(source: Any, target: Any, limit: int) -> tuple[Any, Any]:
    import torch

    count = min(source.size(1), target.size(1))
    source, target = source[:, :count, :], target[:, :count, :]
    if count <= limit:
        return source, target
    indices = torch.linspace(0, count - 1, steps=limit, device=source.device).long()
    return source.index_select(1, indices), target.index_select(1, indices)


def _new_inner(role: str) -> Any:
    _prepare_upstream_imports()
    from modeling import Adapter  # type: ignore

    return load_component(Adapter(HIDDEN_SIZES[role], "ln_res_adapter"), Path(MATH_PROFILE.inner_adapter_checkpoints[role]))


def _new_outer(name: str) -> Any:
    _prepare_upstream_imports()
    from modeling import CrossModelAdapter  # type: ignore

    source, target = OUTER_DIMS[name]
    return load_component(CrossModelAdapter(source, target, "outer_ln_res_adapter"), Path(MATH_PROFILE.outer_adapter_checkpoints[name]))


def _prepare_upstream_imports() -> None:
    for path in (UPSTREAM_ROOT / "inference", UPSTREAM_ROOT):
        value = str(path)
        if value not in sys.path:
            sys.path.insert(0, value)


def _prepare_artifact_tree(root: Path, config: DomainTrainingConfig) -> None:
    for name in ("dataset", "splits", "checkpoints", "metrics", "canaries", "final"):
        (root / name).mkdir(parents=True, exist_ok=True, mode=0o700)
    source_data = REPO_ROOT / "ralfloop_agent/domains/data"
    for name in ("recursive_domain_adapter_cases.jsonl", "recursive_domain_adapter_traces.jsonl", "recursive_domain_adapter_manifest.json"):
        shutil.copy2(source_data / name, root / "dataset" / name)
    shutil.copy2(source_data / "recursive_domain_adapter_splits.json", root / "splits/recursive_domain_adapter_splits.json")
    (root / "training_config.json").write_text(json.dumps(asdict(config), indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _write_manifest(root: Path, config: DomainTrainingConfig, result: dict[str, Any]) -> dict[str, Any]:
    data_manifest = json.loads((REPO_ROOT / "ralfloop_agent/domains/data/recursive_domain_adapter_manifest.json").read_text(encoding="utf-8"))
    checkpoint_hashes = {
        path.relative_to(root).as_posix(): _sha256(path)
        for path in sorted((root / "final").rglob("*.pt"))
    }
    manifest = {
        "profile_id": "recursive_mas_domain_reasoning",
        "profile_version": "v1",
        "base_model_revision": {
            "planner": MATH_SNAPSHOTS["planner"].name,
            "critic": MATH_SNAPSHOTS["critic"].name,
            "solver": MATH_SNAPSHOTS["solver"].name,
        },
        "upstream_revision": "38f7da45c1728747979c7a35bf5f66f17e67b1bb",
        "dataset_hash": data_manifest["dataset_sha256"],
        "split_hash": data_manifest["splits_sha256"],
        "adapter_architecture": {"inner": "ln_res_adapter", "outer": "outer_ln_res_adapter"},
        "hidden_sizes": HIDDEN_SIZES,
        "outer_dimensions": {name: list(value) for name, value in OUTER_DIMS.items()},
        "dtype": config.dtype,
        "training_seed": config.seed,
        "training_steps": result["pilot_steps"],
        "validation_metric": "cosine_plus_0.05_mse",
        "checkpoint_hashes": checkpoint_hashes,
        "base_models_frozen": True,
        "gpu_stagewise": True,
        "feature_enabled": False,
    }
    (root / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return manifest


def _require_safe_runtime(torch: Any) -> None:
    if not torch.cuda.is_available():
        raise RuntimeError("cuda_required_for_bounded_training")
    free, _total = torch.cuda.mem_get_info()
    if free < 6_500 * 1024 * 1024:
        raise RuntimeError("insufficient_free_vram")
    if os.getenv("RALF_RECURSIVE_DOMAIN_REASONING", "0") != "0":
        raise RuntimeError("domain_reasoning_feature_must_remain_disabled")


def _check_timeout(started: float, timeout_seconds: int) -> None:
    if time.monotonic() - started > timeout_seconds:
        raise TimeoutError("bounded_training_timeout")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--artifact-root", type=Path, default=DOMAIN_TRAINING_ROOT)
    parser.add_argument("--dry-config", action="store_true")
    args = parser.parse_args()
    config = DomainTrainingConfig()
    config.validate()
    if args.dry_config:
        print(json.dumps(asdict(config), sort_keys=True))
        return 0
    result = run_training_lab(config, args.artifact_root)
    print(json.dumps({"ok": result["ok"], "pilot_steps": result["pilot_steps"], "manifest": result["manifest"]}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
