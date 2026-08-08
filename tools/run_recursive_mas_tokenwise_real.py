from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import copy
import gc
import hashlib
import inspect
import json
import math
import os
from pathlib import Path
import random
import resource
import statistics
import sys
import time
from typing import Any, Mapping, Sequence

import torch
from torch import nn
import torch.nn.functional as functional
from transformers import AutoModelForCausalLM, AutoTokenizer

REPO = Path("/home/sibilla-cumana/ralfloop_agent_scaffold")
sys.path[:0] = [str(REPO), str(REPO / "tools")]

import run_recursive_mas_qwen3_heldout_benchmark as heldout  # noqa: E402
import run_recursive_mas_provenance_selector_diagnostics as provenance  # noqa: E402
import run_recursive_mas_tokenwise_inner as h1  # noqa: E402

from ralfloop_agent.domains.recursive_mas_domain_dataset import deterministic_splits, generate_dataset  # noqa: E402
from ralfloop_agent.domains.recursive_mas_tokenwise_inner import (  # noqa: E402
    build_positionwise_pairs,
    collapse_report,
    fp32_adamw,
    initialize_critic_inner_tokenwise,
    masked_positionwise_loss,
    nearest_embedding_tokens,
    position_statistics,
    save_tokenwise_checkpoint,
    sha256_file,
    stable_sha256,
)
from ralfloop_agent.domains.recursive_mas_tokenwise_real import (  # noqa: E402
    PROFILE_ID,
    IdentityInner,
    RealTrajectoryConfig,
    ZeroGatedResidualInner,
    aggregate_group_retrieval,
    architecture_manifest,
    config_manifest,
    delta_regularization,
    final_h1b_gate,
    full_raw_teacher_forcing,
    identity_preferred,
    initialize_zero_gated,
    raw_record_valid,
    raw_trajectory_record,
    selection_key,
    token_group,
    validation_constraints,
)


ROOT = REPO / ".ralf_run/recursive_domain_tokenwise_real_v1"
CRITIC_REVISION = h1.CRITIC_REVISION
PLANNER_REVISION = "aa8e72537993ba99e69dfaafa59ed015b17504d1"
RESERVE_B = h1.RESERVE_B
RESERVE_SHA = h1.RESERVE_SHA
GENERATION_CONFIG = {"enable_thinking": False, "temperature": 0, "do_sample": False, "max_new_tokens": 512}
VIEW_SEED = 91427


def dump(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def write_jsonl(path: Path, rows: Sequence[Mapping[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows), encoding="utf-8")


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def rss_bytes() -> int:
    return int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024)


def load_frozen_model(snapshot: Path) -> tuple[Any, Any, float]:
    started = time.monotonic()
    tokenizer = AutoTokenizer.from_pretrained(snapshot, local_files_only=True, trust_remote_code=True, use_fast=True)
    if tokenizer.pad_token_id is None:
        tokenizer.pad_token = tokenizer.eos_token
    model = AutoModelForCausalLM.from_pretrained(
        snapshot, local_files_only=True, trust_remote_code=True, torch_dtype=torch.float16, low_cpu_mem_usage=True
    )
    for parameter in model.parameters():
        parameter.requires_grad = False
    model.eval().to("cuda:0")
    return model, tokenizer, (time.monotonic() - started) * 1000.0


def cases_by_split() -> dict[str, list[dict[str, Any]]]:
    cases, _ = generate_dataset()
    by_id = {str(case["id"]): case for case in cases}
    splits = deterministic_splits(cases)
    return {name: [by_id[case_id] for case_id in splits[name]] for name in ("train", "validation")}


def reordered(case: Mapping[str, Any], view_id: str) -> dict[str, Any]:
    value = copy.deepcopy(case)
    if view_id == "original":
        return value
    if view_id == "reversed":
        value["rules"] = list(reversed(value["rules"]))
        value["sources"] = list(reversed(value["sources"]))
        return value
    if view_id == "seeded":
        for field in ("rules", "sources"):
            random.Random(f"{VIEW_SEED}:{case['id']}:{field}").shuffle(value[field])
        return value
    raise ValueError("h1b_view_invalid")


def _parse_json(raw: str) -> dict[str, Any] | None:
    value = heldout._json_object(raw)
    return dict(value) if isinstance(value, Mapping) else None


def prepare() -> dict[str, Any]:
    config = RealTrajectoryConfig()
    config.validate()
    for name in ("corpus", "cached_trajectories", "checkpoints", "evaluations", "diagnostics", "manifests"):
        (ROOT / name).mkdir(parents=True, exist_ok=True)
    invariants = h1.verify_invariants()
    value = {
        **config_manifest(config),
        "git_head": heldout.sha256_file(REPO / ".git/HEAD"),
        "planner_model": "Qwen/Qwen2.5-3B-Instruct",
        "planner_revision": PLANNER_REVISION,
        "critic_model": "Qwen/Qwen2.5-1.5B-Instruct",
        "critic_revision": CRITIC_REVISION,
        "critic_tokenizer_hash": h1.tokenizer_hash(h1.CRITIC_SNAPSHOT),
        "planner_tokenizer_hash": h1.tokenizer_hash(heldout.QWEN25_3B),
        "generation_config": GENERATION_CONFIG,
        "generation_config_hash": stable_sha256(GENERATION_CONFIG),
        "base_models_frozen": True,
        "pooling": None,
        "outer23_trained": False,
        "previous_checkpoints_overwritten": False,
        "reserve_b": invariants["reserve_b"],
        "feature_flag": invariants["feature_flag"],
        "math_profile_sha256": invariants["math_profile_sha256"],
        "telegram_gate_sha256": invariants["telegram_gate_sha256"],
        "previous_checkpoints": invariants["previous_checkpoints"],
        "classifications_previous": [
            "faithful_tokenwise_contract_verified",
            "gold_trajectory_overfit",
            "teacher_target_distribution_mismatch",
            "tokenwise_adapter_generalization_regression",
        ],
    }
    dump(ROOT / "manifests/lab_manifest.json", value)
    return value


def generate_corpus(split: str, view_id: str) -> dict[str, Any]:
    if split not in {"train", "validation"}:
        raise ValueError("h1b_corpus_split_invalid")
    if split == "validation" and view_id != "original":
        raise ValueError("h1b_validation_view_forbidden")
    cases = [reordered(case, view_id) for case in cases_by_split()[split]]
    progress_path = ROOT / "corpus" / f"{split}_{view_id}_upstream.json"
    progress = json.loads(progress_path.read_text(encoding="utf-8")) if progress_path.exists() else {"cases": {}}
    planner, tokenizer, planner_load = load_frozen_model(heldout.QWEN25_3B)
    try:
        for index, case in enumerate(cases, 1):
            case_id = str(case["id"])
            if case_id in progress["cases"] and progress["cases"][case_id].get("planner"):
                continue
            prompt = heldout.planner_prompt(case)
            generated = heldout.generate_timed(planner, tokenizer, prompt=prompt, max_new_tokens=512)
            parsed = heldout._filter_planner(case, _parse_json(generated["raw"]))
            progress["cases"].setdefault(case_id, {})["planner"] = {
                **generated,
                "structured_output": parsed,
                "prompt_hash": hashlib.sha256(prompt.encode()).hexdigest(),
            }
            if index % 8 == 0:
                dump(progress_path, progress)
                print(json.dumps({"phase": "planner", "split": split, "view": view_id, "done": index, "total": len(cases)}), flush=True)
    finally:
        heldout._unload(planner, tokenizer)
    dump(progress_path, progress)
    critic, tokenizer, critic_load = load_frozen_model(heldout.QWEN25_15B)
    rows: list[dict[str, Any]] = []
    try:
        for index, case in enumerate(cases, 1):
            case_id = str(case["id"])
            prior = progress["cases"].setdefault(case_id, {})
            if not prior.get("critic"):
                prompt = heldout.critic_prompt(case, prior["planner"]["structured_output"])
                generated = heldout.generate_timed(critic, tokenizer, prompt=prompt, max_new_tokens=512)
                parsed = _parse_json(generated["raw"])
                record = raw_trajectory_record(
                    case_id=case_id,
                    view_id=view_id,
                    prompt=prompt,
                    generated=generated,
                    parsed=parsed,
                    model_revision=CRITIC_REVISION,
                    tokenizer_hash=h1.tokenizer_hash(h1.CRITIC_SNAPSHOT),
                    generation_config=GENERATION_CONFIG,
                )
                record["prompt_token_ids"] = heldout._chat_ids(tokenizer, prompt)
                record["domain_id"] = str(case["domain_id"])
                record["category"] = str(case["category"])
                record["allowed_rule_ids"] = [str(item["rule_id"]) for item in case["rules"]]
                record["allowed_source_ids"] = [str(item["source_id"]) for item in case["sources"]]
                prior["critic"] = record
            rows.append(prior["critic"])
            if index % 8 == 0:
                dump(progress_path, progress)
                print(json.dumps({"phase": "critic", "split": split, "view": view_id, "done": index, "total": len(cases)}), flush=True)
    finally:
        heldout._unload(critic, tokenizer)
    dump(progress_path, progress)
    output_path = ROOT / "corpus" / (f"train_real_{view_id}.jsonl" if split == "train" else "validation_real.jsonl")
    write_jsonl(output_path, rows)
    manifest = {
        "split": split,
        "view_id": view_id,
        "records": len(rows),
        "valid_raw_records": sum(raw_record_valid(row) for row in rows),
        "parsed_valid": sum(bool(row["parse_valid"]) for row in rows),
        "parsed_invalid_raw_retained": sum(not row["parse_valid"] and raw_record_valid(row) for row in rows),
        "empty_raw_excluded": sum(bool(row["excluded"]) for row in rows),
        "truncated": sum(bool(row["truncated"]) for row in rows),
        "eos": sum(bool(row["eos_present"]) for row in rows),
        "path": str(output_path),
        "sha256": sha256_file(output_path),
        "planner_load_ms": planner_load,
        "critic_load_ms": critic_load,
        "generation_config_hash": stable_sha256(GENERATION_CONFIG),
    }
    dump(output_path.with_suffix(".manifest.json"), manifest)
    return manifest


def generate_final_a() -> dict[str, Any]:
    frozen_path = ROOT / "manifests/h1b_frozen_manifest.json"
    if not frozen_path.exists() or json.loads(frozen_path.read_text()).get("final_a_executed") is not False:
        raise RuntimeError("h1b_final_a_before_freeze")
    cases = provenance.datasets()["final_a"]
    progress_path = ROOT / "corpus/final_a_upstream.json"
    progress = json.loads(progress_path.read_text()) if progress_path.exists() else {"cases": {}}
    planner, tokenizer, planner_load = load_frozen_model(heldout.QWEN25_3B)
    try:
        for index, case in enumerate(cases, 1):
            case_id = str(case["id"])
            if not progress["cases"].get(case_id, {}).get("planner"):
                prompt = heldout.planner_prompt(case)
                generated = heldout.generate_timed(planner, tokenizer, prompt=prompt, max_new_tokens=512)
                progress["cases"].setdefault(case_id, {})["planner"] = {
                    **generated, "structured_output": heldout._filter_planner(case, _parse_json(generated["raw"])),
                    "prompt_hash": hashlib.sha256(prompt.encode()).hexdigest(),
                }
            if index % 6 == 0:
                dump(progress_path, progress)
                print(json.dumps({"phase": "final_planner", "done": index, "total": len(cases)}), flush=True)
    finally:
        heldout._unload(planner, tokenizer)
    dump(progress_path, progress)
    critic, tokenizer, critic_load = load_frozen_model(heldout.QWEN25_15B)
    rows = []
    try:
        for index, case in enumerate(cases, 1):
            case_id = str(case["id"]); prior = progress["cases"].setdefault(case_id, {})
            if not prior.get("critic"):
                prompt = heldout.critic_prompt(case, prior["planner"]["structured_output"])
                generated = heldout.generate_timed(critic, tokenizer, prompt=prompt, max_new_tokens=512)
                record = raw_trajectory_record(
                    case_id=case_id, view_id="original", prompt=prompt, generated=generated,
                    parsed=_parse_json(generated["raw"]), model_revision=CRITIC_REVISION,
                    tokenizer_hash=h1.tokenizer_hash(h1.CRITIC_SNAPSHOT), generation_config=GENERATION_CONFIG,
                )
                record.update({
                    "prompt_token_ids": heldout._chat_ids(tokenizer, prompt), "domain_id": str(case["domain_id"]),
                    "category": str(case["category"]),
                    "allowed_rule_ids": [str(item["rule_id"]) for item in case["rules"]],
                    "allowed_source_ids": [str(item["source_id"]) for item in case["sources"]],
                })
                prior["critic"] = record
            rows.append(prior["critic"])
            if index % 6 == 0:
                dump(progress_path, progress)
                print(json.dumps({"phase": "final_critic", "done": index, "total": len(cases)}), flush=True)
    finally:
        heldout._unload(critic, tokenizer)
    dump(progress_path, progress)
    path = ROOT / "corpus/final_a_real.jsonl"
    write_jsonl(path, rows)
    manifest = {
        "split": "final_a", "view_id": "original", "records": len(rows),
        "valid_raw_records": sum(raw_record_valid(row) for row in rows),
        "parsed_valid": sum(row["parse_valid"] for row in rows),
        "parsed_invalid_raw_retained": sum(not row["parse_valid"] and raw_record_valid(row) for row in rows),
        "empty_raw_excluded": sum(row["excluded"] for row in rows), "truncated": sum(row["truncated"] for row in rows),
        "eos": sum(row["eos_present"] for row in rows), "path": str(path), "sha256": sha256_file(path),
        "planner_load_ms": planner_load, "critic_load_ms": critic_load,
    }
    dump(path.with_suffix(".manifest.json"), manifest)
    return manifest


def _identifier_ids(tokenizer: Any, record: Mapping[str, Any]) -> set[int]:
    output: set[int] = set()
    for value in [*record["allowed_rule_ids"], *record["allowed_source_ids"]]:
        output.update(int(item) for item in tokenizer(value, add_special_tokens=False)["input_ids"])
    return output


def _structural_ids(tokenizer: Any) -> set[int]:
    text = "{}[],:\" criticisms contradictions rule_application_errors source_provenance_errors strongest_counterargument unresolved_issues"
    return set(int(item) for item in tokenizer(text, add_special_tokens=False)["input_ids"])


def cache_corpus(split: str, view_id: str = "original") -> dict[str, Any]:
    source_path = ROOT / "corpus" / (
        f"train_real_{view_id}.jsonl" if split == "train" else "validation_real.jsonl" if split == "validation" else "final_a_real.jsonl"
    )
    rows = read_jsonl(source_path)
    model, tokenizer, load_ms = h1._load_critic()
    torch.cuda.reset_peak_memory_stats()
    records = []
    cache_name = f"{split}_real_{view_id}" if split == "train" else "validation_real" if split == "validation" else "final_a_real"
    structural_ids = _structural_ids(tokenizer)
    special_ids = {int(value) for value in tokenizer.all_special_ids}
    try:
        embedding = model.get_input_embeddings()
        for index, record in enumerate(rows, 1):
            if not raw_record_valid(record):
                continue
            tensors = full_raw_teacher_forcing(
                prompt_ids=record["prompt_token_ids"],
                raw_output_ids=record["raw_output_token_ids"],
                pad_token_id=int(tokenizer.pad_token_id),
            )
            ids = tensors["full_ids"].to("cuda:0")
            attention = tensors["attention_mask"].to("cuda:0")
            assistant = tensors["assistant_mask"].to("cuda:0")
            with torch.inference_mode():
                output = model(input_ids=ids, attention_mask=attention, output_hidden_states=True, use_cache=False, return_dict=True)
                pairs = build_positionwise_pairs(
                    hidden_states=output.hidden_states[-1], full_ids=ids, input_embeddings=embedding,
                    attention_mask=attention, assistant_mask=assistant,
                )
            mask = pairs["pair_mask"]
            target_ids = pairs["target_ids"][mask]
            identifier_ids = _identifier_ids(tokenizer, record)
            groups = [
                token_group(int(token), tokenizer.decode([int(token)]), special_ids=special_ids,
                            identifier_ids=identifier_ids, structural_ids=structural_ids)
                for token in target_ids.tolist()
            ]
            case_key = f"{record['case_id']}__{view_id}"
            path = ROOT / "cached_trajectories" / cache_name / f"{case_key}.pt"
            payload = {
                "source_hidden": pairs["source_hidden"][mask].float().cpu(),
                "target_embed": pairs["target_embed"][mask].float().cpu(),
                "target_ids": target_ids.cpu(),
                "eos_selected": torch.tensor([int(token) in special_ids for token in target_ids.tolist()]),
            }
            path.parent.mkdir(parents=True, exist_ok=True)
            torch.save(payload, path)
            meta = {
                "case_id": record["case_id"], "view_id": view_id, "category": record["category"],
                "path": str(path), "groups": groups, "input_hash": record["input_hash"],
                "raw_output_hash": record["raw_output_hash"], "parse_valid": record["parse_valid"],
                "selected_positions": int(mask.sum()), "eos_included": bool(payload["eos_selected"].any()),
                "padding_selected": int((mask & ~attention[:, :-1].bool()).sum()),
                "tensor_sha256": {name: h1.tensor_sha256(value) for name, value in payload.items()},
                "pooling": None, "shift": "t_to_t_plus_1",
            }
            dump(path.with_suffix(".json"), meta)
            records.append(meta)
            del output, pairs, ids, attention, assistant
            if index % 24 == 0:
                print(json.dumps({"phase": "cache", "split": cache_name, "done": index, "total": len(rows)}), flush=True)
    finally:
        peak = int(torch.cuda.max_memory_allocated())
        h1.unload(model, tokenizer)
    manifest = {
        "split": cache_name, "records": len(records), "case_keys": [Path(row["path"]).stem for row in records],
        "source_corpus_sha256": sha256_file(source_path),
        "trajectory_hash": stable_sha256([row["tensor_sha256"] for row in records]),
        "all_eos_included": all(row["eos_included"] for row in records),
        "padding_selected": sum(row["padding_selected"] for row in records), "pooling": None,
        "gpu_peak_bytes": peak, "ram_peak_bytes": rss_bytes(), "load_ms": load_ms,
    }
    dump(ROOT / "cached_trajectories" / cache_name / "manifest.json", manifest)
    return manifest


def load_cache(name: str) -> list[dict[str, Any]]:
    root = ROOT / "cached_trajectories" / name
    manifest = json.loads((root / "manifest.json").read_text())
    output = []
    for key in manifest["case_keys"]:
        path = root / f"{key}.pt"
        meta = json.loads(path.with_suffix(".json").read_text())
        payload = torch.load(path, map_location="cpu", weights_only=True)
        for field, expected in meta["tensor_sha256"].items():
            if h1.tensor_sha256(payload[field]) != expected:
                raise RuntimeError(f"h1b_cache_hash_mismatch:{key}:{field}")
        output.append({"metadata": meta, **payload})
    return output


def _sample_indices(records: Sequence[Mapping[str, Any]], limit: int = 96) -> torch.Tensor:
    groups: dict[str, list[int]] = defaultdict(list)
    offset = 0
    for record in records:
        for local, group in enumerate(record["metadata"]["groups"]):
            token = int(record["target_ids"][local])
            groups[group].append(offset + local)
            groups["seen" if token in TRAIN_TOKEN_IDS else "unseen"].append(offset + local)
        offset += int(record["target_ids"].numel())
    selected: set[int] = set()
    for group, values in sorted(groups.items()):
        ranked = sorted(values, key=lambda value: hashlib.sha256(f"{group}:{value}".encode()).hexdigest())
        selected.update(ranked[:limit])
    return torch.tensor(sorted(selected), dtype=torch.long)


TRAIN_TOKEN_IDS: set[int] = set()


def evaluate(module: nn.Module, records: Sequence[Mapping[str, Any]], embedding: torch.Tensor) -> dict[str, Any]:
    module.to("cuda:0", dtype=torch.float32).eval()
    predictions, targets, ids, groups = [], [], [], []
    delta_norms = []
    with torch.inference_mode():
        for record in records:
            source = record["source_hidden"].to("cuda:0", dtype=torch.float32)
            prediction = module(source)
            predictions.append(prediction.cpu())
            targets.append(record["target_embed"].float())
            ids.append(record["target_ids"].long())
            groups.extend(record["metadata"]["groups"])
            delta_norms.append(float((prediction - source).float().norm(dim=-1).mean()))
    prediction = torch.cat(predictions)
    target = torch.cat(targets)
    target_ids = torch.cat(ids)
    metrics = position_statistics(prediction, target)
    metrics["cosine_loss"] = 1.0 - metrics["mean_cosine_similarity"]
    metrics["nan_or_inf"] = bool(not torch.isfinite(prediction).all())
    metrics["delta_norm"] = statistics.mean(delta_norms)
    metrics["alpha_gate"] = float(module.alpha.detach()) if isinstance(module, ZeroGatedResidualInner) else None
    indices = _sample_indices(records)
    sampled_prediction = prediction.index_select(0, indices).to("cuda:0")
    sampled_ids = target_ids.index_select(0, indices)
    sampled_groups = [groups[index] for index in indices.tolist()]
    nearest = nearest_embedding_tokens(sampled_prediction, embedding.to("cuda:0"), top_k=5, position_chunk=256, vocab_chunk=8192)
    metrics["retrieval"] = aggregate_group_retrieval(nearest, sampled_ids, sampled_groups, TRAIN_TOKEN_IDS)
    metrics["retrieval_scope"] = {"positions": int(indices.numel()), "exact_full_vocab": True, "per_group_limit": 96}
    metrics.update(collapse_report({
        **metrics,
        "unique_nearest_token_ratio": metrics["retrieval"]["overall"]["unique_nearest_ratio"],
    }))
    module.to("cpu")
    torch.cuda.empty_cache()
    return metrics


def _position_slice(record: Mapping[str, Any], step: int, offset: int, count: int) -> tuple[torch.Tensor, torch.Tensor]:
    total = int(record["source_hidden"].shape[0])
    if total <= count:
        return record["source_hidden"], record["target_embed"]
    start = int(hashlib.sha256(f"{record['metadata']['case_id']}:{step}:{offset}".encode()).hexdigest(), 16) % total
    indices = torch.tensor([(start + index) % total for index in range(count)], dtype=torch.long)
    return record["source_hidden"].index_select(0, indices), record["target_embed"].index_select(0, indices)


def _schedule(real: Sequence[Mapping[str, Any]], gold: Sequence[Mapping[str, Any]] | None, step: int, config: RealTrajectoryConfig) -> list[Mapping[str, Any]]:
    if gold is None:
        return [real[((step - 1) * config.gradient_accumulation + offset) % len(real)] for offset in range(config.gradient_accumulation)]
    # Four equal-size real chunks + one gold chunk = exact 80/20 positions.
    output = [real[((step - 1) * 4 + offset) % len(real)] for offset in range(4)]
    output.append(gold[(step - 1) % len(gold)])
    return output


def train_variant(variant: str, *, max_steps: int = 400) -> dict[str, Any]:
    global TRAIN_TOKEN_IDS
    if variant not in {"R2", "R3", "R4", "R5"}:
        raise ValueError("h1b_train_variant_invalid")
    config = RealTrajectoryConfig(max_steps=max_steps)
    real = load_cache("train_real_original")
    TRAIN_TOKEN_IDS = {int(token) for record in real for token in record["target_ids"].tolist()}
    validation = load_cache("validation_real")
    gold = h1.load_records("train") if variant in {"R3", "R5"} else None
    module: nn.Module = initialize_zero_gated(config.seed) if variant in {"R4", "R5"} else initialize_critic_inner_tokenwise(config.seed)
    module.to("cuda:0", dtype=torch.float32).train()
    optimizer = fp32_adamw(module, learning_rate=config.learning_rate)
    embedding = h1._embedding_weight()
    identity_metrics = evaluate(IdentityInner(), validation, embedding)
    module.to("cuda:0", dtype=torch.float32).train()
    logs: list[dict[str, Any]] = []
    checkpoints: list[dict[str, Any]] = []
    blocked = None
    torch.cuda.reset_peak_memory_stats()
    for step in range(1, max_steps + 1):
        started = time.monotonic()
        optimizer.zero_grad(set_to_none=True)
        selected = _schedule(real, gold, step, config)
        total_loss = total_cosine = total_mse = total_delta = 0.0
        positions = 0
        accumulation = len(selected)
        for offset, record in enumerate(selected):
            source, target = _position_slice(record, step, offset, config.positions_per_record)
            source = source.to("cuda:0", dtype=torch.float32).unsqueeze(0)
            target = target.to("cuda:0", dtype=torch.float32).unsqueeze(0)
            mask = torch.ones(source.shape[:2], dtype=torch.bool, device="cuda:0")
            prediction = module(source)
            loss, parts = masked_positionwise_loss(prediction, target, mask)
            delta = delta_regularization(module, source)
            combined = loss + (config.delta_weight * delta if variant in {"R4", "R5"} else 0.0)
            (combined / accumulation).backward()
            total_loss += float(combined.detach())
            total_cosine += float(parts["cosine_loss"])
            total_mse += float(parts["mse"])
            total_delta += float(delta.detach())
            positions += int(parts["selected_positions"])
        gradient = float(torch.nn.utils.clip_grad_norm_(module.parameters(), config.gradient_clip))
        if not math.isfinite(gradient):
            blocked = "NaN_or_Inf"
            break
        optimizer.step()
        if any(not torch.isfinite(parameter).all() for parameter in module.parameters()):
            blocked = "NaN_or_Inf"
            break
        row = {
            "step": step, "loss": total_loss / accumulation, "cosine_loss": total_cosine / accumulation,
            "mse": total_mse / accumulation, "delta_regularization": total_delta / accumulation,
            "positions": positions, "gradient_norm": gradient,
            "alpha_gate": float(module.alpha.detach()) if isinstance(module, ZeroGatedResidualInner) else None,
            "gpu_allocated_bytes": int(torch.cuda.memory_allocated()), "gpu_reserved_bytes": int(torch.cuda.memory_reserved()),
            "ram_bytes": rss_bytes(), "step_ms": (time.monotonic() - started) * 1000.0,
        }
        logs.append(row)
        if step % config.evaluate_every == 0:
            metrics = evaluate(module, validation, embedding)
            gate = validation_constraints(metrics, identity_metrics)
            row["validation"] = metrics
            row["gate"] = gate
            path = ROOT / "checkpoints" / variant / f"step_{step:03d}.pt"
            checkpoint = save_tokenwise_checkpoint(module, path, {"variant": variant, "step": step, "validation": metrics, "gate": gate})
            checkpoints.append(checkpoint)
            print(json.dumps({"variant": variant, "step": step, "gate": gate["passed"], "top5": metrics["retrieval"]["overall"]["top5"]}), flush=True)
            if step == config.micro_steps:
                unseen = metrics["retrieval"]["unseen"]["top5"]
                identity_unseen = identity_metrics["retrieval"]["unseen"]["top5"]
                if metrics["sequence_collapse"] or metrics["nan_or_inf"] or unseen < identity_unseen - 0.05:
                    blocked = "micro_canary_retrieval_collapse"
                    break
            module.to("cuda:0", dtype=torch.float32).train()
    module.to("cpu")
    admissible = [checkpoint for checkpoint in checkpoints if checkpoint["gate"]["passed"]]
    selected_checkpoint = max(admissible, key=lambda row: selection_key(row["validation"])) if admissible else None
    result = {
        "variant": variant, "steps": len(logs), "blocked": blocked,
        "loss_initial": logs[0]["loss"] if logs else None, "loss_final": logs[-1]["loss"] if logs else None,
        "identity_validation": identity_metrics, "checkpoints": checkpoints,
        "selected_checkpoint": selected_checkpoint,
        "gpu_peak_bytes": int(torch.cuda.max_memory_allocated()), "ram_peak_bytes": rss_bytes(),
        "nan_or_inf": blocked == "NaN_or_Inf", "oom": False,
    }
    dump(ROOT / "evaluations" / f"{variant}_training.json", result)
    return result


def _load_variant(record: Mapping[str, Any]) -> nn.Module:
    module = initialize_zero_gated() if record["variant"] in {"R4", "R5"} else initialize_critic_inner_tokenwise()
    payload = torch.load(record["path"], map_location="cpu", weights_only=True)
    if sha256_file(Path(record["path"])) != record["sha256"]:
        raise RuntimeError("h1b_checkpoint_hash_mismatch")
    module.load_state_dict(payload["state_dict"], strict=True)
    return module


def distribution() -> dict[str, Any]:
    sets = {
        "train_gold": h1.load_records("train"),
        "train_real": load_cache("train_real_original"),
        "validation_gold": h1.load_records("validation"),
        "validation_real": load_cache("validation_real"),
    }
    counters = {name: Counter(int(token) for record in rows for token in record["target_ids"].tolist()) for name, rows in sets.items()}
    train = counters["train_real"]
    report = {}
    for name, rows in sets.items():
        counter = counters[name]
        lengths = [int(row["target_ids"].numel()) for row in rows]
        unseen_occurrences = sum(count for token, count in counter.items() if token not in train)
        unseen_unique = sum(token not in train for token in counter)
        report[name] = {
            "tokens": sum(counter.values()), "unique_tokens": len(counter),
            "unseen_occurrence_rate_vs_train_real": unseen_occurrences / max(1, sum(counter.values())),
            "unseen_unique_rate_vs_train_real": unseen_unique / max(1, len(counter)),
            "length": {"min": min(lengths), "mean": statistics.mean(lengths), "median": statistics.median(lengths),
                       "p95": sorted(lengths)[max(0, math.ceil(.95 * len(lengths)) - 1)], "max": max(lengths)},
        }
    def js(left: Counter, right: Counter) -> float:
        keys = set(left) | set(right); lt = sum(left.values()) or 1; rt = sum(right.values()) or 1; total = 0.0
        for key in keys:
            p, q = left[key] / lt, right[key] / rt; middle = (p + q) / 2
            if p: total += .5 * p * math.log2(p / middle)
            if q: total += .5 * q * math.log2(q / middle)
        return total
    report["validation_real"]["js_divergence_from_train_real"] = js(train, counters["validation_real"])
    corpus = {}
    for name in ("train_real_original", "validation_real"):
        rows = read_jsonl(ROOT / "corpus" / f"{name}.jsonl")
        corpus[name] = {
            "parse_valid_rate": sum(row["parse_valid"] for row in rows) / len(rows),
            "truncated_rate": sum(row["truncated"] for row in rows) / len(rows),
            "eos_rate": sum(row["eos_present"] for row in rows) / len(rows),
            "empty_raw": sum(row["excluded"] for row in rows),
        }
    value = {"token_distribution": report, "corpus": corpus}
    dump(ROOT / "diagnostics/distribution.json", value)
    return value


def select_and_freeze() -> dict[str, Any]:
    global TRAIN_TOKEN_IDS
    train = load_cache("train_real_original")
    TRAIN_TOKEN_IDS = {int(token) for record in train for token in record["target_ids"].tolist()}
    validation = load_cache("validation_real")
    embedding = h1._embedding_weight()
    baselines = {
        "R0": evaluate(IdentityInner(), validation, embedding),
        "R1": evaluate(initialize_critic_inner_tokenwise(), validation, embedding),
    }
    candidates = []
    for variant in ("R2", "R3", "R4", "R5"):
        path = ROOT / "evaluations" / f"{variant}_training.json"
        result = json.loads(path.read_text())
        if result["selected_checkpoint"]:
            candidates.append(result["selected_checkpoint"])
    if candidates:
        selected = max(candidates, key=lambda row: selection_key(row["validation"]))
        inner_mode = "trained_tokenwise"
        metrics = selected["validation"]
    else:
        selected = None
        inner_mode = "identity"
        metrics = baselines["R0"]
    identity_choice = identity_preferred(baselines["R0"], [row["validation"] for row in candidates]) if candidates else True
    if identity_choice:
        selected = None; inner_mode = "identity"; metrics = baselines["R0"]
    train_manifest = json.loads((ROOT / "cached_trajectories/train_real_original/manifest.json").read_text())
    validation_manifest = json.loads((ROOT / "cached_trajectories/validation_real/manifest.json").read_text())
    module = IdentityInner() if inner_mode == "identity" else _load_variant(selected)
    frozen = {
        "profile_id": PROFILE_ID, "inner_mode": inner_mode, "selected_variant": selected["variant"] if selected else "R0",
        "checkpoint": selected, "checkpoint_sha256": selected["sha256"] if selected else None,
        "architecture": architecture_manifest(module), "train_corpus_hash": train_manifest["source_corpus_sha256"],
        "validation_corpus_hash": validation_manifest["source_corpus_sha256"],
        "prompt_hash": stable_sha256([row["prompt_hash"] for row in read_jsonl(ROOT / "corpus/validation_real.jsonl")]),
        "model_revision": CRITIC_REVISION, "tokenizer_hash": h1.tokenizer_hash(h1.CRITIC_SNAPSHOT),
        "generation_config_hash": stable_sha256(GENERATION_CONFIG),
        "mask_implementation_hash": hashlib.sha256(inspect.getsource(build_positionwise_pairs).encode()).hexdigest(),
        "loss_config": {"cosine": 1.0, "mse": .1, "delta": RealTrajectoryConfig().delta_weight},
        "selection_metrics": metrics, "identity_metrics": baselines["R0"],
        "final_a_executed": False, "outer23_trained": False,
    }
    dump(ROOT / "manifests/h1b_frozen_manifest.json", frozen)
    frozen["manifest_sha256"] = sha256_file(ROOT / "manifests/h1b_frozen_manifest.json")
    dump(ROOT / "evaluations/selection.json", {"baselines": baselines, "candidates": candidates, "frozen": frozen})
    return frozen


def evaluate_final_a() -> dict[str, Any]:
    global TRAIN_TOKEN_IDS
    frozen = json.loads((ROOT / "manifests/h1b_frozen_manifest.json").read_text())
    if frozen.get("final_a_executed") is not False:
        raise RuntimeError("h1b_final_a_freeze_invalid")
    if not (ROOT / "cached_trajectories/final_a_real/manifest.json").exists():
        raise RuntimeError("h1b_final_a_cache_missing")
    train = load_cache("train_real_original")
    TRAIN_TOKEN_IDS = {int(token) for record in train for token in record["target_ids"].tolist()}
    records = load_cache("final_a_real")
    embedding = h1._embedding_weight()
    identity = evaluate(IdentityInner(), records, embedding)
    if frozen["inner_mode"] == "identity":
        candidate = identity
        gate = {"passed": False, "classification": "identity_mapping_preferred"}
        inner_mode = "identity"
        checkpoint = None
        h2 = True
    else:
        module = _load_variant(frozen["checkpoint"])
        candidate = evaluate(module, records, embedding)
        contract = {"positionwise": True, "no_pooling": True, "shift": True, "assistant_mask": True,
                    "eos": all(row["metadata"]["eos_included"] for row in records),
                    "padding": all(row["metadata"]["padding_selected"] == 0 for row in records)}
        gate = final_h1b_gate(candidate, identity, contract)
        inner_mode = "trained_tokenwise" if gate["passed"] else None
        checkpoint = None
        h2 = bool(gate["passed"])
        if gate["passed"]:
            checkpoint = save_tokenwise_checkpoint(
                module, ROOT / "final/critic_inner_tokenwise_real_v1.pt",
                {"profile_id": PROFILE_ID, "variant": frozen["selected_variant"], "source_sha256": frozen["checkpoint_sha256"],
                 "validation_metrics": frozen["selection_metrics"], "final_a_metrics": candidate,
                 "outer23_trained": False, "previous_checkpoints_overwritten": False},
            )
    result = {
        "final_a_executed": True, "identity": identity, "candidate": candidate, "gate": gate,
        "classification": gate["classification"], "inner_mode": inner_mode or "laboratory_failed",
        "checkpoint": checkpoint, "h2_authorized": h2, "outer23_trained": False,
    }
    dump(ROOT / "evaluations/final_a.json", result)
    if h2:
        profile = {
            "profile_id": PROFILE_ID, "enabled": False, "registered": False, "inner_mode": inner_mode,
            "critic_inner_checkpoint": checkpoint["path"] if checkpoint else None,
            "critic_inner_checkpoint_sha256": checkpoint["sha256"] if checkpoint else None,
            "outer23": "new_training_required", "solver_inner_bypassed": True,
        }
        dump(REPO / "ralfloop_agent/domains/recursive_mas_domain_tokenwise_real_v1.json", profile)
    return result


def finalize() -> dict[str, Any]:
    manifest = json.loads((ROOT / "manifests/lab_manifest.json").read_text())
    final = json.loads((ROOT / "evaluations/final_a.json").read_text())
    invariants = h1.verify_invariants()
    value = {
        "problem": "gold_trajectory_overfit",
        "previous_classification_corrected": manifest["classifications_previous"],
        "result": final,
        "training_outer23": False, "training_base_models": False,
        "reserve_b": invariants["reserve_b"], "feature_flag": invariants["feature_flag"],
        "math_profile_sha256": invariants["math_profile_sha256"],
        "telegram_gate_sha256": invariants["telegram_gate_sha256"],
        "previous_checkpoints": invariants["previous_checkpoints"],
    }
    dump(ROOT / "manifests/final_report.json", value)
    return value


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("phase", choices=("prepare", "corpus", "cache", "distribution", "train", "freeze", "final-corpus", "final-cache", "final-eval", "finalize"))
    parser.add_argument("--split", choices=("train", "validation"))
    parser.add_argument("--view", default="original", choices=("original", "reversed", "seeded"))
    parser.add_argument("--variant", choices=("R2", "R3", "R4", "R5"))
    parser.add_argument("--steps", type=int, default=400)
    args = parser.parse_args()
    if args.phase == "prepare": result = prepare()
    elif args.phase == "corpus": result = generate_corpus(args.split or "train", args.view)
    elif args.phase == "cache": result = cache_corpus(args.split or "train", args.view)
    elif args.phase == "distribution": result = distribution()
    elif args.phase == "train": result = train_variant(args.variant or "R2", max_steps=args.steps)
    elif args.phase == "freeze": result = select_and_freeze()
    elif args.phase == "final-corpus": result = generate_final_a()
    elif args.phase == "final-cache": result = cache_corpus("final_a")
    elif args.phase == "final-eval": result = evaluate_final_a()
    else: result = finalize()
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
