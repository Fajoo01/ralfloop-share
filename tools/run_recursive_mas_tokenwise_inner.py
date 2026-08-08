from __future__ import annotations

import argparse
from collections import defaultdict
import gc
import hashlib
import inspect
import json
import math
import os
from pathlib import Path
import resource
import statistics
import sys
import time
from typing import Any, Iterable, Mapping, Sequence

import torch
from torch import nn
import torch.nn.functional as functional
from transformers import AutoModelForCausalLM, AutoTokenizer

REPO = Path("/home/sibilla-cumana/ralfloop_agent_scaffold")
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tools"))
import run_recursive_mas_provenance_selector_diagnostics as provenance  # noqa: E402
import run_recursive_mas_qwen3_heldout_benchmark as heldout  # noqa: E402

from ralfloop_agent.domains.recursive_mas_domain_dataset import (  # noqa: E402
    CATEGORIES,
    deterministic_splits,
    generate_dataset,
)
from ralfloop_agent.domains.recursive_mas_qwen3_solver_eval import CANARY_CASE_IDS  # noqa: E402
from ralfloop_agent.domains.recursive_mas_tokenwise_inner import (  # noqa: E402
    PROFILE_ID,
    TokenwiseInnerConfig,
    assistant_mask_from_prefix,
    build_positionwise_pairs,
    collapse_report,
    contract_manifest,
    faithful_one_round_latent,
    fp32_adamw,
    generation_layout,
    h1_gate,
    initialize_critic_inner_tokenwise,
    masked_positionwise_loss,
    nearest_embedding_tokens,
    position_statistics,
    retrieval_statistics,
    save_tokenwise_checkpoint,
    load_tokenwise_checkpoint,
    sha256_file,
    stable_sha256,
)


ROOT = REPO / ".ralf_run/recursive_domain_tokenwise_inner_v1"
CRITIC_SNAPSHOT = Path("/home/sibilla-cumana/RecursiveMAS/.hf-domain-instruct-v1/hub/models--Qwen--Qwen2.5-1.5B-Instruct/snapshots/989aa7980e4cf806f80c7fef2b1adb7bc71aa306")
QWEN3_SNAPSHOT = heldout.QWEN3
CRITIC_REVISION = "989aa7980e4cf806f80c7fef2b1adb7bc71aa306"
QWEN3_REVISION = "70d244cc86ccca08cf5af4e1e306ecf908b1ad5e"
FINAL_A_PATH = REPO / "ralfloop_agent/domains/data/recursive_domain_external_holdout/final_a.jsonl"
RESERVE_B = REPO / ".ralf_run/recursive_domain_external_holdout/reserve_b.jsonl"
RESERVE_SHA = "44e70370dd5f4322bd19a5e654f24b24c945522f9474c0c5d3a6332533e2ed6e"
VALIDATION_UPSTREAM = REPO / ".ralf_run/recursive_domain_provenance_diagnostics/validation_upstream.json"
FINAL_UPSTREAM = REPO / ".ralf_run/recursive_domain_external_holdout/upstream.json"
OLD_FINAL = REPO / ".ralf_run/recursive_domain_qwen3_micro_overfit/final"
OLD_HASHES = {
    "planner_inner": "865956b1589b642c9a7af8dcc8422196a57b61966543bb2b6c8504e5c56b867a",
    "outer12": "a6194560d9933391a23fea22ec82e6b69e3c862cd634aa5028179c603a50e935",
    "critic_inner": "8590c8fad24698f02a7a2eae61523bf688c60eb6d63fbb52c54ebe28cd2f631f",
    "outer23": "f99fc9c3f9b00755afbd65e265fd39bc45738ecd98b2792afadeb96aab655811",
    "solver_inner": "c0be6a6d5dcc9ce5dd1c4da6819383adcf7f638ff84ce38628ca2f9c5c4d60ec",
}


def dump(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def rss_bytes() -> int:
    return int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024)


def tensor_sha256(value: torch.Tensor) -> str:
    tensor = value.detach().cpu().contiguous()
    digest = hashlib.sha256()
    digest.update(str(tensor.dtype).encode())
    digest.update(json.dumps(list(tensor.shape), separators=(",", ":")).encode())
    digest.update(tensor.view(torch.uint8).numpy().tobytes())
    return digest.hexdigest()


def tokenizer_hash(snapshot: Path) -> str:
    names = ("merges.txt", "tokenizer.json", "tokenizer_config.json", "vocab.json")
    return stable_sha256({name: sha256_file(snapshot / name) for name in names if (snapshot / name).exists()})


def reserve_seal() -> dict[str, Any]:
    stat = RESERVE_B.stat()
    digest = hashlib.sha256()
    lines = 0
    with RESERVE_B.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
            lines += chunk.count(b"\n")
    value = {
        "path": str(RESERVE_B),
        "permissions": oct(stat.st_mode & 0o777)[2:].zfill(3),
        "records": lines,
        "sha256": digest.hexdigest(),
        "semantically_read": False,
        "executed": False,
    }
    value["sealed"] = value["permissions"] == "600" and lines == 24 and value["sha256"] == RESERVE_SHA
    if not value["sealed"]:
        raise RuntimeError("reserve_b_seal_invalid")
    return value


def verify_invariants() -> dict[str, Any]:
    checkpoints = {}
    for name, expected in OLD_HASHES.items():
        path = OLD_FINAL / f"{name}.pt"
        actual = sha256_file(path)
        if actual != expected:
            raise RuntimeError(f"previous_checkpoint_hash_mismatch:{name}")
        checkpoints[name] = {"path": str(path), "sha256": actual}
    return {
        "previous_checkpoints": checkpoints,
        "feature_flag": os.getenv("RALF_RECURSIVE_DOMAIN_REASONING", "0"),
        "reserve_b": reserve_seal(),
        "math_profile_sha256": sha256_file(REPO / "ralfloop_agent/domains/recursive_mas_profiles.py"),
        "telegram_gate_sha256": sha256_file(REPO / "ralfloop_agent/domains/domain_approval_executor.py"),
    }


def original_data() -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]], dict[str, list[str]]]:
    cases, traces = generate_dataset()
    return (
        {str(case["id"]): case for case in cases},
        {str(trace["case_id"]): trace for trace in traces},
        deterministic_splits(cases),
    )


def split_examples(split: str) -> list[dict[str, Any]]:
    cases, traces, splits = original_data()
    if split in {"train", "validation"}:
        output = []
        for case_id in splits[split]:
            case = cases[case_id]
            trace = traces[case_id]
            planner = trace["planner_target"]
            critic = trace["critic_target"]
            output.append(
                {
                    "case": case,
                    "prompt": heldout.critic_prompt(case, planner),
                    "assistant": json.dumps(critic, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                    "target_kind": "gold_structured_critic",
                }
            )
        return output
    if split == "micro":
        output = []
        for case_id in CANARY_CASE_IDS:
            case = cases[case_id]
            trace = traces[case_id]
            output.append(
                {
                    "case": case,
                    "prompt": heldout.critic_prompt(case, trace["planner_target"]),
                    "assistant": json.dumps(trace["critic_target"], ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                    "target_kind": "gold_structured_critic_micro",
                }
            )
        return output
    if split == "final_a":
        cases_final = provenance.datasets()["final_a"]
        upstream = json.loads(FINAL_UPSTREAM.read_text(encoding="utf-8"))
        output = []
        for case in cases_final:
            row = upstream["cases"][case["id"]]
            planner = row["planner"]["structured_output"] or {}
            critic = row["critic"]["structured_output"] or {}
            output.append(
                {
                    "case": case,
                    "prompt": heldout.critic_prompt(case, planner),
                    "assistant": json.dumps(critic, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                    "target_kind": "observed_structured_critic",
                }
            )
        return output
    raise ValueError("tokenwise_split_invalid")


def _load_critic() -> tuple[Any, Any, float]:
    started = time.monotonic()
    tokenizer = AutoTokenizer.from_pretrained(CRITIC_SNAPSHOT, local_files_only=True, trust_remote_code=True, use_fast=True)
    if tokenizer.pad_token_id is None:
        tokenizer.pad_token = tokenizer.eos_token
    model = AutoModelForCausalLM.from_pretrained(
        CRITIC_SNAPSHOT,
        local_files_only=True,
        trust_remote_code=True,
        torch_dtype=torch.float16,
        low_cpu_mem_usage=True,
    )
    for parameter in model.parameters():
        parameter.requires_grad = False
    model.eval().to("cuda:0")
    return model, tokenizer, (time.monotonic() - started) * 1000.0


def unload(model: Any, tokenizer: Any) -> None:
    model.to("cpu")
    del model, tokenizer
    gc.collect()
    torch.cuda.empty_cache()
    torch.cuda.synchronize()


def _pad_teacher_forcing(
    tokenizer: Any, prompt: str, assistant: str
) -> tuple[torch.Tensor, torch.Tensor, torch.Tensor, int]:
    prompt_raw = heldout._chat_ids(tokenizer, prompt)
    full_raw = heldout._chat_ids(tokenizer, prompt, assistant)
    prompt_ids = torch.tensor(prompt_raw, dtype=torch.long).unsqueeze(0)
    raw = torch.tensor(full_raw, dtype=torch.long).unsqueeze(0)
    raw_assistant = assistant_mask_from_prefix(raw, prompt_ids)
    padding = (-raw.shape[1]) % 8
    if padding:
        pad = torch.full((1, padding), int(tokenizer.pad_token_id), dtype=torch.long)
        full_ids = torch.cat((raw, pad), dim=1)
        attention = torch.cat((torch.ones_like(raw), torch.zeros_like(pad)), dim=1)
        assistant_mask = torch.cat((raw_assistant, torch.zeros_like(pad, dtype=torch.bool)), dim=1)
    else:
        full_ids, attention, assistant_mask = raw, torch.ones_like(raw), raw_assistant
    return full_ids, attention, assistant_mask, len(prompt_raw)


def _save_cache(split: str, case: Mapping[str, Any], payload: Mapping[str, torch.Tensor], metadata: Mapping[str, Any]) -> dict[str, Any]:
    case_id = str(case["id"])
    path = ROOT / "cached_trajectories" / split / f"{case_id}.pt"
    path.parent.mkdir(parents=True, exist_ok=True)
    values = {name: value.detach().cpu() for name, value in payload.items()}
    torch.save(values, path)
    record = {
        **dict(metadata),
        "case_id": case_id,
        "domain_id": str(case["domain_id"]),
        "category": str(case["category"]),
        "path": str(path),
        "tensor_sha256": {name: tensor_sha256(value) for name, value in values.items()},
    }
    dump(path.with_suffix(".json"), record)
    return record


def extract_split(split: str) -> dict[str, Any]:
    examples = split_examples(split)
    model, tokenizer, load_ms = _load_critic()
    torch.cuda.reset_peak_memory_stats()
    records = []
    try:
        embedding = model.get_input_embeddings()
        eos_ids = {int(tokenizer.eos_token_id)} if tokenizer.eos_token_id is not None else set()
        for index, example in enumerate(examples, 1):
            case = example["case"]
            full_ids, attention, assistant_mask, prompt_length = _pad_teacher_forcing(
                tokenizer, example["prompt"], example["assistant"]
            )
            ids_gpu = full_ids.to("cuda:0")
            attention_gpu = attention.to("cuda:0")
            assistant_gpu = assistant_mask.to("cuda:0")
            started = time.monotonic()
            with torch.inference_mode():
                output = model(
                    input_ids=ids_gpu,
                    attention_mask=attention_gpu,
                    output_hidden_states=True,
                    use_cache=False,
                    return_dict=True,
                )
                pairs = build_positionwise_pairs(
                    hidden_states=output.hidden_states[-1],
                    full_ids=ids_gpu,
                    input_embeddings=embedding,
                    attention_mask=attention_gpu,
                    assistant_mask=assistant_gpu,
                )
            mask = pairs["pair_mask"]
            source = pairs["source_hidden"][mask].float()
            target = pairs["target_embed"][mask].float()
            target_ids = pairs["target_ids"][mask]
            positions = mask.nonzero(as_tuple=False)[:, 1]
            eos_selected = torch.tensor([int(token) in eos_ids for token in target_ids.tolist()], device="cuda:0")
            record = _save_cache(
                split,
                case,
                {
                    "source_hidden": source,
                    "target_embed": target,
                    "target_ids": target_ids,
                    "positions": positions,
                    "full_ids": full_ids,
                    "attention_mask": attention,
                    "assistant_mask": assistant_mask,
                    "pair_mask": mask.cpu(),
                    "eos_selected": eos_selected,
                },
                {
                    "model_id": "Qwen/Qwen2.5-1.5B-Instruct",
                    "model_revision": CRITIC_REVISION,
                    "tokenizer_hash": tokenizer_hash(CRITIC_SNAPSHOT),
                    "input_hash": stable_sha256({"prompt": example["prompt"], "assistant": example["assistant"]}),
                    "target_kind": example["target_kind"],
                    "full_sequence_length": int(full_ids.shape[1]),
                    "prompt_length": prompt_length,
                    "assistant_token_count": int(assistant_mask.sum()),
                    "selected_position_count": int(mask.sum()),
                    "eos_included": bool(eos_selected.any()),
                    "padding_count": int((attention == 0).sum()),
                    "padding_selected": int((mask & ~attention_gpu[:, :-1].bool()).sum()),
                    "shift": "t_to_t_plus_1",
                    "pooling": None,
                    "wall_ms": (time.monotonic() - started) * 1000.0,
                    "ordinal": index,
                },
            )
            records.append(record)
            del output, pairs, source, target, target_ids, ids_gpu, attention_gpu, assistant_gpu
            if index % 24 == 0:
                print(json.dumps({"split": split, "cached": index, "total": len(examples)}), flush=True)
    finally:
        peak = int(torch.cuda.max_memory_allocated())
        unload(model, tokenizer)
    manifest = {
        "split": split,
        "count": len(records),
        "case_ids": [record["case_id"] for record in records],
        "dataset_hash": stable_sha256([record["input_hash"] for record in records]),
        "trajectory_hash": stable_sha256([record["tensor_sha256"] for record in records]),
        "load_ms": load_ms,
        "gpu_peak_bytes": peak,
        "ram_peak_bytes": rss_bytes(),
        "contract": contract_manifest(),
        "all_eos_included": all(record["eos_included"] for record in records),
        "padding_selected": sum(record["padding_selected"] for record in records),
        "pooling": None,
    }
    dump(ROOT / "cached_trajectories" / split / "manifest.json", manifest)
    return manifest


def load_records(split: str) -> list[dict[str, Any]]:
    root = ROOT / "cached_trajectories" / split
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    output = []
    for case_id in manifest["case_ids"]:
        path = root / f"{case_id}.pt"
        metadata = json.loads(path.with_suffix(".json").read_text(encoding="utf-8"))
        payload = torch.load(path, map_location="cpu", weights_only=True)
        for name, expected in metadata["tensor_sha256"].items():
            if tensor_sha256(payload[name]) != expected:
                raise RuntimeError(f"tokenwise_cache_hash_mismatch:{split}:{case_id}:{name}")
        output.append({"metadata": metadata, **payload})
    return output


def _embedding_weight() -> torch.Tensor:
    from safetensors import safe_open

    index_path = CRITIC_SNAPSHOT / "model.safetensors.index.json"
    key = "model.embed_tokens.weight"
    if index_path.exists():
        mapping = json.loads(index_path.read_text(encoding="utf-8"))["weight_map"]
        filename = mapping[key]
    else:
        filename = "model.safetensors"
    with safe_open(CRITIC_SNAPSHOT / filename, framework="pt", device="cpu") as handle:
        return handle.get_tensor(key)


def _selected_retrieval_indices(records: Sequence[Mapping[str, Any]], limit: int) -> tuple[torch.Tensor, list[str]]:
    categories: list[str] = []
    eos: list[bool] = []
    for record in records:
        count = int(record["target_ids"].numel())
        categories.extend([str(record["metadata"]["category"])] * count)
        eos.extend(bool(value) for value in record["eos_selected"].tolist())
    by_category: dict[str, list[int]] = defaultdict(list)
    for index, category in enumerate(categories):
        by_category[category].append(index)
    selected: list[int] = []
    for category in sorted(by_category):
        ranked = sorted(by_category[category], key=lambda idx: hashlib.sha256(f"{category}:{idx}".encode()).hexdigest())
        chosen = ranked[:limit]
        chosen.extend(index for index in ranked if eos[index] and index not in chosen)
        selected.extend(chosen)
    selected = sorted(dict.fromkeys(selected))
    return torch.tensor(selected, dtype=torch.long), [categories[index] for index in selected]


def evaluate_adapter(
    module: nn.Module,
    records: Sequence[Mapping[str, Any]],
    *,
    embedding_weight: torch.Tensor | None = None,
    retrieval_limit: int = 64,
) -> dict[str, Any]:
    module.to("cuda:0", dtype=torch.float32).eval()
    predictions, targets, target_ids = [], [], []
    with torch.inference_mode():
        for record in records:
            source = record["source_hidden"].to("cuda:0", dtype=torch.float32)
            prediction = module(source)
            predictions.append(prediction.cpu())
            targets.append(record["target_embed"].float())
            target_ids.append(record["target_ids"].long())
    prediction = torch.cat(predictions)
    target = torch.cat(targets)
    ids = torch.cat(target_ids)
    cosine_values = functional.cosine_similarity(prediction.float(), target.float(), dim=-1)
    metrics = {
        "selected_positions": int(prediction.shape[0]),
        "cosine_loss": float(1.0 - cosine_values.mean()),
        **position_statistics(prediction, target),
        "nan_or_inf": bool(not torch.isfinite(prediction).all()),
    }
    if embedding_weight is not None:
        indices, sample_categories = _selected_retrieval_indices(records, retrieval_limit)
        sampled_prediction = prediction.index_select(0, indices).to("cuda:0")
        sampled_ids = ids.index_select(0, indices)
        weight = embedding_weight.to("cuda:0")
        nearest = nearest_embedding_tokens(
            sampled_prediction,
            weight,
            top_k=5,
            position_chunk=256,
            vocab_chunk=8192,
        )
        retrieval = retrieval_statistics(nearest, sampled_ids)
        metrics.update(retrieval)
        per_category = {}
        for category in sorted(set(sample_categories)):
            category_indices = torch.tensor([i for i, value in enumerate(sample_categories) if value == category])
            per_category[category] = retrieval_statistics(
                nearest.index_select(0, category_indices), sampled_ids.index_select(0, category_indices)
            )
        eos_mask = torch.tensor([int(token) == 151645 for token in sampled_ids.tolist()], dtype=torch.bool)
        metrics["eos_alignment"] = (
            retrieval_statistics(nearest[eos_mask], sampled_ids[eos_mask]) if eos_mask.any() else None
        )
        shuffled = target.roll(1, 0)
        metrics["token_order_margin"] = float(
            cosine_values.mean() - functional.cosine_similarity(prediction.float(), shuffled.float(), dim=-1).mean()
        )
        metrics["retrieval_scope"] = {
            "positions": int(indices.numel()),
            "per_category_limit": retrieval_limit,
            "vocab_size": int(embedding_weight.shape[0]),
            "exact_full_vocab": True,
        }
        metrics["token_position_accuracy_per_category"] = per_category
        del weight, sampled_prediction, nearest
        torch.cuda.empty_cache()
    metrics.update(collapse_report(metrics))
    module.to("cpu")
    return metrics


def _alignment_score(module: nn.Module, records: Sequence[Mapping[str, Any]]) -> dict[str, float]:
    result = evaluate_adapter(module, records, embedding_weight=None)
    return {"cosine_loss": float(result["cosine_loss"]), "mse": float(result["mse"]), "mean_cosine_similarity": float(result["mean_cosine_similarity"])}


def _train(
    module: nn.Module,
    train_records: Sequence[Mapping[str, Any]],
    *,
    steps: int,
    checkpoint_every: int,
    evaluate_every: int,
    checkpoint_root: Path,
    learning_rate: float,
    validation_records: Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    config = TokenwiseInnerConfig()
    module.to("cuda:0", dtype=torch.float32).train()
    for parameter in module.parameters():
        parameter.requires_grad = True
    optimizer = fp32_adamw(module, learning_rate=learning_rate)
    logs = []
    best = None
    stale = 0
    torch.cuda.reset_peak_memory_stats()
    for step in range(1, steps + 1):
        started = time.monotonic()
        optimizer.zero_grad(set_to_none=True)
        totals = {"loss": 0.0, "cosine_loss": 0.0, "mse": 0.0, "positions": 0}
        for offset in range(config.gradient_accumulation):
            record = train_records[((step - 1) * config.gradient_accumulation + offset) % len(train_records)]
            source = record["source_hidden"].to("cuda:0", dtype=torch.float32).unsqueeze(0)
            target = record["target_embed"].to("cuda:0", dtype=torch.float32).unsqueeze(0)
            mask = torch.ones(source.shape[:2], dtype=torch.bool, device="cuda:0")
            prediction = module(source)
            loss, parts = masked_positionwise_loss(prediction, target, mask)
            (loss / config.gradient_accumulation).backward()
            totals["loss"] += float(loss.detach())
            totals["cosine_loss"] += float(parts["cosine_loss"].detach())
            totals["mse"] += float(parts["mse"].detach())
            totals["positions"] += int(parts["selected_positions"])
        gradient_norm = float(torch.nn.utils.clip_grad_norm_(module.parameters(), config.gradient_clip))
        if not math.isfinite(gradient_norm):
            raise RuntimeError("tokenwise_non_finite_gradient")
        optimizer.step()
        if any(not torch.isfinite(parameter).all() for parameter in module.parameters()):
            raise RuntimeError("tokenwise_non_finite_weight")
        row = {
            "step": step,
            "loss": totals["loss"] / config.gradient_accumulation,
            "cosine_loss": totals["cosine_loss"] / config.gradient_accumulation,
            "mse": totals["mse"] / config.gradient_accumulation,
            "selected_positions": totals["positions"],
            "gradient_norm": gradient_norm,
            "allocated_gpu_bytes": int(torch.cuda.memory_allocated()),
            "reserved_gpu_bytes": int(torch.cuda.memory_reserved()),
            "ram_process_bytes": rss_bytes(),
            "step_ms": (time.monotonic() - started) * 1000.0,
        }
        logs.append(row)
        if step % checkpoint_every == 0:
            save_tokenwise_checkpoint(module, checkpoint_root / f"step_{step:03d}.pt", {"step": step, "intermediate": True})
        if validation_records is not None and step % evaluate_every == 0:
            validation = _alignment_score(module, validation_records)
            row["validation"] = validation
            score = validation["cosine_loss"] + 0.1 * validation["mse"]
            if best is None or score < best["score"] - 1e-7:
                best = {"step": step, "score": score, "metrics": validation}
                stale = 0
            else:
                stale += 1
            print(json.dumps({"step": step, "validation": validation, "stale": stale}), flush=True)
            module.to("cuda:0", dtype=torch.float32).train()
            if stale >= config.validation_patience:
                break
    module.to("cpu")
    return {
        "steps": len(logs),
        "loss_initial": logs[0]["loss"],
        "loss_final": logs[-1]["loss"],
        "cosine_initial": logs[0]["cosine_loss"],
        "cosine_final": logs[-1]["cosine_loss"],
        "mse_initial": logs[0]["mse"],
        "mse_final": logs[-1]["mse"],
        "logs": logs,
        "best": best,
        "gpu_peak_bytes": int(torch.cuda.max_memory_allocated()),
        "ram_peak_bytes": rss_bytes(),
        "nan_or_inf": False,
        "oom": False,
    }


def audit_and_prepare() -> dict[str, Any]:
    config = TokenwiseInnerConfig()
    config.validate()
    ROOT.mkdir(parents=True, exist_ok=True)
    invariants = verify_invariants()
    manifest = {
        **contract_manifest(config),
        "git_head_before": "52a55f6943b929917286351694a73660de49a399",
        "critic_model": "Qwen/Qwen2.5-1.5B-Instruct",
        "critic_revision": CRITIC_REVISION,
        "solver_model": "Qwen/Qwen3-1.7B",
        "solver_revision": QWEN3_REVISION,
        "tokenizer_hash": tokenizer_hash(CRITIC_SNAPSHOT),
        "invariants": invariants,
        "base_models_frozen": True,
        "trained_components": ["critic_inner_tokenwise_v1"],
        "outer23_trained": False,
        "solver_inner_trained": False,
        "reserve_b_semantically_read": False,
        "reserve_b_executed": False,
    }
    dump(ROOT / "manifest.json", manifest)
    split_manifests = {}
    for split in ("train", "validation", "micro"):
        split_manifests[split] = extract_split(split)
    dump(ROOT / "cached_trajectories" / "summary.json", split_manifests)
    return {"manifest": manifest, "splits": split_manifests}


def micro_overfit() -> dict[str, Any]:
    records = load_records("micro")
    embedding = _embedding_weight()
    fresh = initialize_critic_inner_tokenwise()
    old = heldout._load_adapters("trained")["critic_inner"]
    fresh_metrics = evaluate_adapter(fresh, records, embedding_weight=embedding)
    pooled_metrics = evaluate_adapter(old, records, embedding_weight=embedding)
    trained = initialize_critic_inner_tokenwise()
    training = _train(
        trained,
        records,
        steps=TokenwiseInnerConfig().micro_steps,
        checkpoint_every=TokenwiseInnerConfig().checkpoint_every_micro,
        evaluate_every=TokenwiseInnerConfig().evaluate_every_micro,
        checkpoint_root=ROOT / "checkpoints" / "micro",
        learning_rate=TokenwiseInnerConfig().micro_learning_rate,
    )
    trained_metrics = evaluate_adapter(trained, records, embedding_weight=embedding)
    passed = bool(
        training["loss_final"] < training["loss_initial"]
        and trained_metrics["mean_cosine_similarity"] > fresh_metrics["mean_cosine_similarity"]
        and trained_metrics["top1_token_accuracy"] > fresh_metrics["top1_token_accuracy"]
        and trained_metrics["top5_token_accuracy"] > fresh_metrics["top5_token_accuracy"]
        and not trained_metrics["sequence_collapse"]
        and not trained_metrics["nan_or_inf"]
    )
    result = {
        "passed": passed,
        "classification": "micro_tokenwise_alignment_passed" if passed else "tokenwise_alignment_not_trainable",
        "fresh": fresh_metrics,
        "old_pooled_adapter_positionwise": pooled_metrics,
        "old_pooled_comparison_note": "Old pointwise module accepts token positions, but its weights were trained on pooled one-slot inputs.",
        "trained": trained_metrics,
        "training": training,
        "contract": {
            "shift_t_to_t_plus_1": True,
            "assistant_only": True,
            "eos_included": all(record["metadata"]["eos_included"] for record in records),
            "padding_excluded": all(record["metadata"]["padding_selected"] == 0 for record in records),
            "order_preserved": True,
            "no_pooling": True,
        },
    }
    dump(ROOT / "evaluations" / "micro_overfit.json", result)
    return result


def _freeze_manifest(best_record: Mapping[str, Any]) -> dict[str, Any]:
    module_file = REPO / "ralfloop_agent/domains/recursive_mas_tokenwise_inner.py"
    runner_file = REPO / "tools/run_recursive_mas_tokenwise_inner.py"
    train_manifest = json.loads((ROOT / "cached_trajectories/train/manifest.json").read_text())
    validation_manifest = json.loads((ROOT / "cached_trajectories/validation/manifest.json").read_text())
    value = {
        "profile_id": PROFILE_ID,
        "selected_checkpoint": dict(best_record),
        "prompt_hash": stable_sha256([example["prompt"] for example in split_examples("validation")]),
        "train_hash": train_manifest["dataset_hash"],
        "validation_hash": validation_manifest["dataset_hash"],
        "tokenizer_hash": tokenizer_hash(CRITIC_SNAPSHOT),
        "model_revision": CRITIC_REVISION,
        "adapter_config": contract_manifest(),
        "mask_implementation_hash": hashlib.sha256(inspect.getsource(build_positionwise_pairs).encode()).hexdigest(),
        "training_config": vars(TokenwiseInnerConfig()),
        "module_sha256": sha256_file(module_file),
        "runner_sha256": sha256_file(runner_file),
        "final_a_observed": False,
    }
    dump(ROOT / "frozen_manifest_before_final_a.json", value)
    value["manifest_sha256"] = sha256_file(ROOT / "frozen_manifest_before_final_a.json")
    return value


def bounded_training() -> dict[str, Any]:
    micro = json.loads((ROOT / "evaluations/micro_overfit.json").read_text())
    if not micro["passed"]:
        result = {"executed": False, "classification": "tokenwise_alignment_not_trainable", "reason": "micro_gate_failed"}
        dump(ROOT / "evaluations/bounded_training.json", result)
        return result
    train_records = load_records("train")
    validation_records = load_records("validation")
    embedding = _embedding_weight()
    fresh = initialize_critic_inner_tokenwise()
    fresh_validation = evaluate_adapter(fresh, validation_records, embedding_weight=embedding)
    trained = initialize_critic_inner_tokenwise()
    training = _train(
        trained,
        train_records,
        steps=TokenwiseInnerConfig().bounded_steps,
        checkpoint_every=TokenwiseInnerConfig().checkpoint_every_bounded,
        evaluate_every=TokenwiseInnerConfig().evaluate_every_bounded,
        checkpoint_root=ROOT / "checkpoints" / "bounded",
        learning_rate=TokenwiseInnerConfig().bounded_learning_rate,
        validation_records=validation_records,
    )
    if not training["best"]:
        raise RuntimeError("tokenwise_validation_checkpoint_missing")
    best_step = int(training["best"]["step"])
    best_path = ROOT / "checkpoints" / "bounded" / f"step_{best_step:03d}.pt"
    best_record = json.loads(best_path.with_suffix(".json").read_text())
    selected = initialize_critic_inner_tokenwise()
    load_tokenwise_checkpoint(selected, best_path, best_record["sha256"])
    validation = evaluate_adapter(selected, validation_records, embedding_weight=embedding)
    contract = {
        "shift_t_to_t_plus_1": True,
        "assistant_only": True,
        "eos_included": all(record["metadata"]["eos_included"] for record in validation_records),
        "padding_excluded": all(record["metadata"]["padding_selected"] == 0 for record in validation_records),
        "order_preserved": True,
        "no_pooling": True,
    }
    validation_gate = h1_gate(fresh_validation, validation, contract)
    frozen = _freeze_manifest(best_record)
    result = {
        "executed": True,
        "fresh_validation": fresh_validation,
        "validation": validation,
        "validation_gate": validation_gate,
        "contract": contract,
        "training": training,
        "best_checkpoint": best_record,
        "freeze": frozen,
    }
    dump(ROOT / "evaluations/bounded_training_pre_final.json", result)
    if not validation_gate["passed"]:
        result.update({"passed": False, "classification": validation_gate["classification"], "final_a_executed": False})
        dump(ROOT / "evaluations/bounded_training.json", result)
        return result
    final_manifest = extract_split("final_a")
    final_records = load_records("final_a")
    fresh_final = evaluate_adapter(initialize_critic_inner_tokenwise(), final_records, embedding_weight=embedding)
    final_metrics = evaluate_adapter(selected, final_records, embedding_weight=embedding)
    coherent = bool(
        final_metrics["cosine_loss"] < fresh_final["cosine_loss"]
        and final_metrics["mse"] < fresh_final["mse"]
        and final_metrics["top5_token_accuracy"] >= fresh_final["top5_token_accuracy"]
        and not final_metrics["sequence_collapse"]
        and not final_metrics["nan_or_inf"]
    )
    passed = bool(validation_gate["passed"] and coherent)
    result.update(
        {
            "passed": passed,
            "classification": "faithful_tokenwise_inner_alignment_passed" if passed else "tokenwise_alignment_not_trainable",
            "final_a_executed": True,
            "final_a_manifest": final_manifest,
            "fresh_final_a": fresh_final,
            "final_a": final_metrics,
            "final_a_coherent": coherent,
        }
    )
    if passed:
        final_path = ROOT / "final" / "critic_inner_tokenwise_v1.pt"
        checkpoint = save_tokenwise_checkpoint(
            selected,
            final_path,
            {
                "profile_id": PROFILE_ID,
                "step": best_step,
                "train_hash": frozen["train_hash"],
                "validation_hash": frozen["validation_hash"],
                "final_a_hash": final_manifest["dataset_hash"],
                "tokenizer_hash": frozen["tokenizer_hash"],
                "model_revision": CRITIC_REVISION,
                "mask_hash": frozen["mask_implementation_hash"],
                "validation_metrics": validation,
                "final_a_metrics": final_metrics,
                "outer23_trained": False,
                "previous_checkpoints_overwritten": False,
            },
        )
        result["checkpoint"] = checkpoint
        dump(ROOT / "final" / "critic_inner_tokenwise_v1_manifest.json", checkpoint)
    dump(ROOT / "evaluations/bounded_training.json", result)
    return result


def _load_selected_tokenwise() -> nn.Module:
    result = json.loads((ROOT / "evaluations/bounded_training.json").read_text())
    record = result.get("checkpoint") or result.get("best_checkpoint")
    if not record:
        raise RuntimeError("tokenwise_validation_checkpoint_missing")
    module = initialize_critic_inner_tokenwise()
    load_tokenwise_checkpoint(module, Path(record["path"]), record["sha256"])
    return module


def _aggregate_ablation(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    result = heldout.aggregate(rows)
    result.update(
        {
            "format_errors": sum(row.get("error_classification") == "format_error" for row in rows),
            "semantic_errors": sum(row.get("error_classification") == "solver_semantic_error" for row in rows),
            "adapter_transfer_errors": sum(row.get("error_classification") == "adapter_transfer_error" for row in rows),
            "latent_length": sorted({int(row["latent_length"]) for row in rows}),
            "gpu_peak_bytes": max(int(row["gpu_peak_bytes"]) for row in rows),
            "wall_ms": sum(float(row["timings"]["wall_total_ms"]) for row in rows),
        }
    )
    return result


def ablate_final_path() -> dict[str, Any]:
    tokenwise = _load_selected_tokenwise().to("cuda:0", dtype=torch.float32).eval()
    adapters = heldout._load_adapters("trained").to("cuda:0", dtype=torch.float32).eval()
    model, tokenizer, load_ms = heldout._load_model(QWEN3_SNAPSHOT)
    torch.cuda.reset_peak_memory_stats()
    variants = ("P0", "P1", "P2_cap16", "P2_cap32", "P3_cap16", "P3_cap32")
    output: dict[str, Any] = {"splits": {}}
    previous_root = heldout.ROOT
    heldout.ROOT = ROOT / "ablation_runtime"
    try:
        with torch.inference_mode():
            for split, upstream_path in (("validation", VALIDATION_UPSTREAM), ("final_a", FINAL_UPSTREAM)):
                cases = provenance.datasets()[split]
                upstream = json.loads(upstream_path.read_text(encoding="utf-8"))
                records = {record["metadata"]["case_id"]: record for record in load_records(split)}
                rows: dict[str, list[dict[str, Any]]] = {variant: [] for variant in variants}
                for case in cases:
                    case_id = str(case["id"])
                    source = records[case_id]["source_hidden"].to("cuda:0", dtype=torch.float32).unsqueeze(0)
                    pooled = source.mean(dim=1, keepdim=True)
                    old_bridge = adapters["outer23"](adapters["critic_inner"](pooled))
                    new_bridge = faithful_one_round_latent(tokenwise, adapters["outer23"], source)
                    latent_values = {
                        "P0": adapters["solver_inner"](old_bridge),
                        "P1": old_bridge,
                        "P2_cap16": adapters["solver_inner"](new_bridge[:, :16]),
                        "P2_cap32": adapters["solver_inner"](new_bridge[:, :32]),
                        "P3_cap16": new_bridge[:, :16],
                        "P3_cap32": new_bridge[:, :32],
                    }
                    row_upstream = upstream["cases"][case_id]
                    packet = heldout._packet(case, upstream)
                    prompt = heldout.solver_prompt(case, row_upstream["critic"]["structured_output"], packet)
                    prompt_length = len(heldout._chat_ids(tokenizer, prompt))
                    for variant in variants:
                        latent = latent_values[variant]
                        generated = heldout.generate_timed(model, tokenizer, prompt=prompt, latent=latent)
                        scored = heldout._score_solver_row(
                            case,
                            upstream,
                            generated,
                            variant,
                            load_ms=load_ms / max(1, len(cases) * len(variants)),
                            gpu_peak_bytes=int(torch.cuda.max_memory_allocated()),
                        )
                        layout = generation_layout(prefix_length=prompt_length, latent_length=int(latent.shape[1]), suffix_length=0)
                        scored.update(
                            {
                                "latent_length": int(latent.shape[1]),
                                "solver_inner_bypassed": variant.startswith("P1") or variant.startswith("P3"),
                                "position_ids": "derived_monotonic_from_all_ones_attention_mask",
                                "attention_mask_length": layout["attention_mask_length"],
                                "inputs_embeds_length": layout["inputs_embeds_length"],
                                "generation_boundary": layout["generation_boundary"],
                            }
                        )
                        rows[variant].append(scored)
                        dump(ROOT / "evaluations" / "final_path_ablation" / split / variant / f"{case_id}.json", scored)
                output["splits"][split] = {variant: _aggregate_ablation(values) for variant, values in rows.items()}
                print(json.dumps({"ablation_split": split, "complete": True}), flush=True)
    finally:
        heldout.ROOT = previous_root
        adapters.to("cpu")
        tokenwise.to("cpu")
        heldout._unload(model, tokenizer)
    output.update(
        {
            "solver_inner_audit": {
                "old_path": "receiver-side transform before final decode",
                "classification": "solver_inner_position_incompatible_with_upstream_sequential_contract",
                "faithful_one_round_path": "critic_inner_tokenwise_v1 -> old outer23 -> Qwen3",
                "outer31": "excluded",
            },
            "diagnostic_only": True,
            "old_outer23_retrained": False,
            "p2_p3_failure_does_not_invalidate_h1": True,
        }
    )
    dump(ROOT / "evaluations" / "final_path_ablation" / "summary.json", output)
    return output


def final_summary() -> dict[str, Any]:
    bounded = json.loads((ROOT / "evaluations/bounded_training.json").read_text())
    ablation_path = ROOT / "evaluations/final_path_ablation/summary.json"
    value = {
        "profile_id": PROFILE_ID,
        "h1": bounded,
        "ablation": json.loads(ablation_path.read_text()) if ablation_path.exists() else None,
        "h2_authorized": bool(bounded.get("passed")),
        "h2_executed": False,
        "outer23_trained": False,
        "solver_inner_bypassed_in_faithful_path": True,
        "outer31": "excluded",
        "reserve_b": reserve_seal(),
        "training_executed": "critic_inner_tokenwise_v1_only",
        "production_modified": False,
        "models_downloaded": False,
    }
    dump(ROOT / "summary.json", value)
    return value


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("phase", choices=("prepare", "micro", "train", "ablate", "summary", "all"))
    args = parser.parse_args()
    if args.phase in {"prepare", "all"}:
        audit_and_prepare()
    if args.phase in {"micro", "all"}:
        micro_overfit()
    if args.phase in {"train", "all"}:
        bounded_training()
    if args.phase in {"ablate", "all"}:
        ablate_final_path()
    if args.phase in {"summary", "all"}:
        print(json.dumps(final_summary(), ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
