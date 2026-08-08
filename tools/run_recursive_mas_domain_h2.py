from __future__ import annotations

import argparse
from collections import Counter
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
from typing import Any, Mapping, Sequence

import torch
import torch.nn.functional as functional
from torch import nn
from transformers import AutoModelForCausalLM, AutoTokenizer


REPO = Path("/home/sibilla-cumana/ralfloop_agent_scaffold")
sys.path[:0] = [str(REPO), str(REPO / "tools")]

import run_recursive_mas_qwen3_heldout_benchmark as heldout  # noqa: E402
import run_recursive_mas_qwen3_micro_overfit as old_micro  # noqa: E402
import run_recursive_mas_tokenwise_inner as h1  # noqa: E402
import run_recursive_mas_tokenwise_real as h1b  # noqa: E402

from ralfloop_agent.domains.recursive_mas_domain_dataset import deterministic_splits, generate_dataset  # noqa: E402
from ralfloop_agent.domains.recursive_mas_domain_h2 import (  # noqa: E402
    ALLOWED_CAPS,
    H1B_FREEZE_SHA256,
    H1B_SHA256,
    H2Config,
    OrderedOuter23,
    architecture_manifest,
    h2_gate,
    initialize_outer23,
    lab_manifest,
    layout_audit,
    micro_gate,
    ordered_cap,
    reserve_gate,
    response_only_labels,
    selection_key,
    sha256_file,
    stable_sha256,
)
from ralfloop_agent.domains.recursive_mas_external_benchmark import (  # noqa: E402
    aggregate_external,
    enrich_score,
    load_external_cases,
    verify_runner_freeze,
)
from ralfloop_agent.domains.recursive_mas_qwen3_heldout_benchmark import (  # noqa: E402
    aggregate,
    input_payload,
    score_output,
)
from ralfloop_agent.domains.recursive_mas_qwen3_solver_eval import (  # noqa: E402
    CANARY_CASE_IDS,
    build_real_packet,
    evaluate_solver_final,
)
from ralfloop_agent.domains.recursive_mas_tokenwise_inner import build_positionwise_pairs  # noqa: E402
from ralfloop_agent.domains.recursive_mas_tokenwise_real import (  # noqa: E402
    ZeroGatedResidualInner,
    full_raw_teacher_forcing,
)


ROOT = REPO / ".ralf_run/recursive_mas_domain_h2_outer23_v1"
H1B_ROOT = REPO / ".ralf_run/recursive_domain_tokenwise_real_v1"
H1B_CHECKPOINT = H1B_ROOT / "final/critic_inner_tokenwise_real_v1.pt"
H1B_FREEZE = H1B_ROOT / "manifests/h1b_frozen_manifest.json"
QWEN3 = old_micro.QWEN3
QWEN25_15B = old_micro.QWEN25_15B
FINAL_A = REPO / ".ralf_run/recursive_domain_external_holdout/final_a.jsonl"
RESERVE_B = REPO / ".ralf_run/recursive_domain_external_holdout/reserve_b.jsonl"
RESERVE_SHA256 = "44e70370dd5f4322bd19a5e654f24b24c945522f9474c0c5d3a6332533e2ed6e"
EXTERNAL_ROOT = REPO / ".ralf_run/recursive_domain_external_holdout"
DIRECT_TARGETS = old_micro.DIRECT_TARGETS
RUNNER_FREEZE = EXTERNAL_ROOT / "runner_frozen_manifest.json"
GENERATION_CONFIG = {"enable_thinking": False, "temperature": 0, "do_sample": False, "max_new_tokens": 512}
PROMPT_MARKER = "IMMUTABLE EVIDENCE PACKET:"


def dump(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def rss_bytes() -> int:
    return int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024)


def tensor_sha256(value: torch.Tensor) -> str:
    tensor = value.detach().cpu().contiguous()
    digest = hashlib.sha256()
    digest.update(str(tensor.dtype).encode())
    digest.update(json.dumps(list(tensor.shape), separators=(",", ":")).encode())
    digest.update(tensor.view(torch.uint8).numpy().tobytes())
    return digest.hexdigest()


def git_head() -> str:
    import subprocess

    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=REPO, check=True, capture_output=True, text=True
    ).stdout.strip()


def _reserve_metadata() -> dict[str, Any]:
    stat = RESERVE_B.stat()
    return {
        "path": str(RESERVE_B),
        "sha256": sha256_file(RESERVE_B),
        "records": sum(1 for _ in RESERVE_B.open("rb")),
        "permissions": oct(stat.st_mode & 0o777),
        "semantically_read": False,
        "execution_count": 0,
    }


def _load_h1b() -> ZeroGatedResidualInner:
    if sha256_file(H1B_CHECKPOINT) != H1B_SHA256:
        raise RuntimeError("h2_critic_inner_sha_mismatch")
    module = ZeroGatedResidualInner().to(dtype=torch.float32)
    payload = torch.load(H1B_CHECKPOINT, map_location="cpu", weights_only=True)
    module.load_state_dict(payload["state_dict"], strict=True)
    module.eval()
    for parameter in module.parameters():
        parameter.requires_grad = False
    return module


def _model(snapshot: Path, *, training: bool) -> tuple[Any, Any, dict[str, Any]]:
    started = time.monotonic()
    tokenizer = AutoTokenizer.from_pretrained(snapshot, local_files_only=True, trust_remote_code=True, use_fast=True)
    if tokenizer.pad_token_id is None:
        tokenizer.pad_token = tokenizer.eos_token
    model = AutoModelForCausalLM.from_pretrained(
        snapshot,
        local_files_only=True,
        trust_remote_code=True,
        dtype=torch.float16,
        low_cpu_mem_usage=True,
    )
    for parameter in model.parameters():
        parameter.requires_grad = False
    model.eval().to("cuda:0")
    if training:
        model.gradient_checkpointing_enable(gradient_checkpointing_kwargs={"use_reentrant": False})
        model.config.use_cache = False
    return model, tokenizer, {
        "load_ms": (time.monotonic() - started) * 1000.0,
        "parameters": sum(parameter.numel() for parameter in model.parameters()),
        "trainable_parameters": sum(parameter.numel() for parameter in model.parameters() if parameter.requires_grad),
        "dtype": str(next(model.parameters()).dtype),
        "device": str(next(model.parameters()).device),
    }


def _unload(model: Any, tokenizer: Any) -> None:
    try:
        model.to("cpu")
    finally:
        del model, tokenizer
        gc.collect()
        torch.cuda.empty_cache()
        torch.cuda.synchronize()


def _case_maps() -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]], dict[str, list[str]]]:
    cases, traces = generate_dataset()
    return (
        {str(case["id"]): case for case in cases},
        {str(trace["case_id"]): trace for trace in traces},
        deterministic_splits(cases),
    )


def _canonical_target(trace: Mapping[str, Any]) -> str:
    target = trace["solver_target"]
    lines = [f"POSITION: {str(target['position']).strip()}"]
    lines.extend(f"SUPPORT: {str(item['text']).strip()}" for item in target["supporting_arguments"][:3])
    lines.extend(f"COUNTER: {str(item['text']).strip()}" for item in target["counterarguments"][:3])
    lines.extend(f"UNCERTAINTY: {str(item).strip()}" for item in target.get("uncertainties", [])[:3])
    lines.extend(f"ALTERNATIVE: {str(item).strip()}" for item in target.get("alternative_interpretations", [])[:2])
    lines.extend(
        (
            f"RECOMMENDATION: {str(target['recommendation']).strip()}",
            f"CONFIDENCE: {float(target['confidence']):.2f}",
            f"HUMAN: {str(bool(target['human_decision_required'])).lower()}",
            "END",
        )
    )
    return "\n".join(lines)


def _source_cache(split: str) -> Path:
    name = {"train": "train_real_original", "validation": "validation_real", "final_a": "final_a_real"}[split]
    return H1B_ROOT / "cached_trajectories" / name


def _corpus_path(split: str) -> Path:
    return H1B_ROOT / "corpus" / {
        "train": "train_real_original.jsonl",
        "validation": "validation_real.jsonl",
        "final_a": "final_a_real.jsonl",
    }[split]


def _upstream_path(split: str) -> Path:
    return H1B_ROOT / "corpus" / {
        "train": "train_original_upstream.json",
        "validation": "validation_original_upstream.json",
        "final_a": "final_a_upstream.json",
    }[split]


def _cache_h1b_split(split: str) -> dict[str, Any]:
    if split not in {"train", "validation", "final_a"}:
        raise ValueError("h2_cache_split_invalid")
    if split == "final_a" and not all((ROOT / "manifests" / f"h2_cap{cap}_frozen.json").exists() for cap in ALLOWED_CAPS):
        raise RuntimeError("h2_final_a_cache_before_freeze")
    source_root = _source_cache(split)
    source_manifest = json.loads((source_root / "manifest.json").read_text())
    corpus = {str(row["case_id"]): row for row in read_jsonl(_corpus_path(split))}
    destination = ROOT / "cached_trajectories" / split
    module = _load_h1b().to("cuda:0")
    rows = []
    with torch.inference_mode():
        for case_key in source_manifest["case_keys"]:
            source_path = source_root / f"{case_key}.pt"
            source_meta = json.loads(source_path.with_suffix(".json").read_text())
            payload = torch.load(source_path, map_location="cpu", weights_only=True)
            source = payload["source_hidden"].float().unsqueeze(0)
            inner = module(source.to("cuda:0")).cpu()
            case_id = str(source_meta["case_id"])
            output_path = destination / f"{case_id}.pt"
            output_path.parent.mkdir(parents=True, exist_ok=True)
            torch.save({"critic_hidden": source, "critic_inner_h1b": inner}, output_path)
            raw = corpus[case_id]
            meta = {
                "case_id": case_id,
                "raw_output_hash": raw["raw_output_hash"],
                "raw_output_token_ids": raw["raw_output_token_ids"],
                "assistant_mask": raw["assistant_token_mask"],
                "eos": raw["eos_present"],
                "attention_mask": [1] * int(source.shape[1]),
                "source_hidden_hash": tensor_sha256(source),
                "critic_inner_output_hash": tensor_sha256(inner),
                "sequence_before_cap": int(inner.shape[1]),
                "position_order": list(range(int(inner.shape[1]))),
                "pooling": None,
                "h1b_sha256": H1B_SHA256,
                "path": str(output_path),
            }
            dump(output_path.with_suffix(".json"), meta)
            rows.append(meta)
    module.to("cpu")
    torch.cuda.empty_cache()
    manifest = {
        "split": split,
        "records": len(rows),
        "case_ids": [row["case_id"] for row in rows],
        "source_manifest_sha256": sha256_file(source_root / "manifest.json"),
        "source_corpus_sha256": sha256_file(_corpus_path(split)),
        "trajectory_hash": stable_sha256([row["critic_inner_output_hash"] for row in rows]),
        "order_preserved": all(row["position_order"] == list(range(row["sequence_before_cap"])) for row in rows),
        "pooling": None,
    }
    dump(destination / "manifest.json", manifest)
    return manifest


def _load_cached(split: str, case_id: str) -> tuple[torch.Tensor, dict[str, Any]]:
    path = ROOT / "cached_trajectories" / split / f"{case_id}.pt"
    meta = json.loads(path.with_suffix(".json").read_text())
    payload = torch.load(path, map_location="cpu", weights_only=True)
    inner = payload["critic_inner_h1b"].float()
    if tensor_sha256(inner) != meta["critic_inner_output_hash"]:
        raise RuntimeError("h2_cached_inner_hash_mismatch")
    return inner, meta


def _split_records(split: str) -> list[dict[str, Any]]:
    cases, traces, splits = _case_maps()
    if split in {"train", "validation"}:
        ids = splits[split]
        upstream = json.loads(_upstream_path(split).read_text())["cases"]
        output = []
        for case_id in ids:
            case = cases[case_id]
            row = upstream[case_id]
            critic_record = row["critic"]
            critic = critic_record.get("parsed_critic_output") or {}
            planner = row["planner"]["structured_output"] or {}
            packet = build_real_packet(case, planner, critic)
            output.append(
                {
                    "case": case,
                    "trace": traces[case_id],
                    "critic": critic,
                    "planner": planner,
                    "packet": packet,
                    "prompt": heldout.solver_prompt(case, critic, packet),
                    "target": _canonical_target(traces[case_id]),
                }
            )
        return output
    if split == "micro":
        upstream = json.loads((ROOT / "cached_trajectories/micro/upstream.json").read_text())["cases"]
        output = []
        for case_id in CANARY_CASE_IDS:
            case = cases[case_id]
            row = upstream[case_id]
            critic = row["critic"]["structured_output"] or {}
            planner = row["planner"]["structured_output"] or {}
            packet = build_real_packet(case, planner, critic)
            output.append(
                {
                    "case": case,
                    "trace": traces[case_id],
                    "critic": critic,
                    "planner": planner,
                    "packet": packet,
                    "prompt": heldout.solver_prompt(case, critic, packet),
                    "target": (DIRECT_TARGETS / case_id / "raw_sanitized.txt").read_text().strip(),
                }
            )
        return output
    if split == "final_a":
        cases_external = load_external_cases(FINAL_A)
        upstream = json.loads(_upstream_path("final_a").read_text())["cases"]
        output = []
        for case in cases_external:
            row = upstream[str(case["id"])]
            critic_record = row["critic"]
            critic = critic_record.get("parsed_critic_output") or {}
            planner = row["planner"]["structured_output"] or {}
            packet = build_real_packet(case, planner, critic)
            output.append(
                {
                    "case": case,
                    "trace": {"case_id": case["id"]},
                    "critic": critic,
                    "planner": planner,
                    "packet": packet,
                    "prompt": heldout.solver_prompt(case, critic, packet),
                }
            )
        return output
    raise ValueError("h2_record_split_invalid")


def _cache_micro_real() -> dict[str, Any]:
    cases, _, _ = _case_maps()
    context = old_micro.context()
    critic_model, tokenizer, load = _model(QWEN25_15B, training=False)
    inner = _load_h1b().to("cuda:0")
    upstream: dict[str, Any] = {"cases": {}}
    records = []
    torch.cuda.reset_peak_memory_stats()
    try:
        embedding = critic_model.get_input_embeddings()
        for case_id in CANARY_CASE_IDS:
            case = cases[case_id]
            planner = context["upstream"]["cases"][case_id]["planner"]["structured_output"] or {}
            prompt = heldout.critic_prompt(case, planner)
            generated = heldout.generate_timed(critic_model, tokenizer, prompt=prompt, max_new_tokens=512)
            raw_ids = [int(value) for value in generated["token_ids"]]
            parsed = heldout._json_object(generated["raw"]) or {}
            prompt_ids = heldout._chat_ids(tokenizer, prompt)
            tensors = full_raw_teacher_forcing(
                prompt_ids=prompt_ids,
                raw_output_ids=raw_ids,
                pad_token_id=int(tokenizer.pad_token_id),
            )
            ids = tensors["full_ids"].to("cuda:0")
            attention = tensors["attention_mask"].to("cuda:0")
            assistant = tensors["assistant_mask"].to("cuda:0")
            with torch.inference_mode():
                result = critic_model(
                    input_ids=ids,
                    attention_mask=attention,
                    output_hidden_states=True,
                    use_cache=False,
                    return_dict=True,
                )
                pairs = build_positionwise_pairs(
                    hidden_states=result.hidden_states[-1],
                    full_ids=ids,
                    input_embeddings=embedding,
                    attention_mask=attention,
                    assistant_mask=assistant,
                )
                source = pairs["source_hidden"][pairs["pair_mask"]].float().unsqueeze(0)
                inner_value = inner(source).cpu()
            path = ROOT / "cached_trajectories/micro" / f"{case_id}.pt"
            path.parent.mkdir(parents=True, exist_ok=True)
            torch.save({"critic_hidden": source.cpu(), "critic_inner_h1b": inner_value}, path)
            meta = {
                "case_id": case_id,
                "raw_output_hash": hashlib.sha256(generated["raw"].encode()).hexdigest(),
                "raw_output_token_ids": raw_ids,
                "assistant_mask": [True] * len(raw_ids),
                "eos": bool(generated["eos"]),
                "attention_mask": [1] * int(source.shape[1]),
                "source_hidden_hash": tensor_sha256(source),
                "critic_inner_output_hash": tensor_sha256(inner_value),
                "sequence_before_cap": int(inner_value.shape[1]),
                "position_order": list(range(int(inner_value.shape[1]))),
                "pooling": None,
                "h1b_sha256": H1B_SHA256,
                "path": str(path),
            }
            dump(path.with_suffix(".json"), meta)
            records.append(meta)
            upstream["cases"][case_id] = {
                "planner": {"structured_output": planner},
                "critic": {**generated, "structured_output": parsed},
            }
            del result, pairs, ids, attention, assistant
    finally:
        inner.to("cpu")
        peak = int(torch.cuda.max_memory_allocated())
        _unload(critic_model, tokenizer)
    dump(ROOT / "cached_trajectories/micro/upstream.json", upstream)
    manifest = {
        "split": "micro",
        "records": len(records),
        "case_ids": list(CANARY_CASE_IDS),
        "trajectory_hash": stable_sha256([row["critic_inner_output_hash"] for row in records]),
        "order_preserved": True,
        "pooling": None,
        "gpu_peak_bytes": peak,
        "ram_peak_bytes": rss_bytes(),
        "critic_load": load,
    }
    dump(ROOT / "cached_trajectories/micro/manifest.json", manifest)
    return manifest


def _rendered_parts(tokenizer: Any, prompt: str) -> tuple[torch.Tensor, torch.Tensor, dict[str, Any]]:
    rendered = tokenizer.apply_chat_template(
        [{"role": "user", "content": prompt}],
        tokenize=False,
        add_generation_prompt=True,
        enable_thinking=False,
    )
    index = rendered.find(PROMPT_MARKER)
    if index <= 0:
        raise RuntimeError("h2_evidence_packet_marker_missing")
    prefix_text, suffix_text = rendered[:index], rendered[index:]
    prefix = torch.tensor(tokenizer(prefix_text, add_special_tokens=False)["input_ids"], dtype=torch.long).unsqueeze(0)
    suffix = torch.tensor(tokenizer(suffix_text, add_special_tokens=False)["input_ids"], dtype=torch.long).unsqueeze(0)
    return prefix, suffix, {
        "prompt_hash": hashlib.sha256(prompt.encode()).hexdigest(),
        "rendered_hash": hashlib.sha256(rendered.encode()).hexdigest(),
        "evidence_packet_text_present": PROMPT_MARKER in suffix_text,
        "enable_thinking": False,
    }


def _training_input(
    model: Any,
    tokenizer: Any,
    *,
    prompt: str,
    target: str,
    latent: torch.Tensor,
) -> tuple[torch.Tensor, torch.Tensor, torch.Tensor, dict[str, Any]]:
    prefix_ids, suffix_ids, prompt_audit = _rendered_parts(tokenizer, prompt)
    target_values = tokenizer(target, add_special_tokens=False)["input_ids"] + [int(tokenizer.eos_token_id)]
    target_ids = torch.tensor(target_values, dtype=torch.long).unsqueeze(0)
    prefix_ids = prefix_ids.to("cuda:0")
    suffix_ids = suffix_ids.to("cuda:0")
    target_ids = target_ids.to("cuda:0")
    embed = model.get_input_embeddings()
    with torch.no_grad():
        prefix = embed(prefix_ids)
        suffix = embed(suffix_ids)
        response = embed(target_ids)
    latent = latent.to(dtype=prefix.dtype)
    full = torch.cat((prefix, latent, suffix, response), dim=1)
    prompt_text_tokens = int(prefix.shape[1] + suffix.shape[1])
    labels = response_only_labels(prompt_text_tokens, int(latent.shape[1]), target_ids)
    attention = torch.ones(full.shape[:2], dtype=torch.long, device="cuda:0")
    position_ids = torch.arange(full.shape[1], device="cuda:0", dtype=torch.long).unsqueeze(0)
    audit = {
        **prompt_audit,
        **layout_audit(
            prefix_tokens=int(prefix.shape[1]),
            latent_tokens=int(latent.shape[1]),
            suffix_tokens=int(suffix.shape[1]),
            response_tokens=int(response.shape[1]),
        ),
        "prompt_loss_tokens": int((labels[:, : prompt_text_tokens + latent.shape[1]] != -100).sum()),
        "latent_loss_tokens": int((labels[:, prefix.shape[1] : prefix.shape[1] + latent.shape[1]] != -100).sum()),
        "response_loss_tokens": int((labels[:, -target_ids.shape[1] :] != -100).sum()),
        "attention_mask_all_one": bool(attention.all()),
        "position_ids_last": int(position_ids[0, -1]),
    }
    return full, labels, position_ids, audit


def _generate(
    model: Any,
    tokenizer: Any,
    *,
    prompt: str,
    latent: torch.Tensor | None,
) -> dict[str, Any]:
    started = time.monotonic()
    if latent is None:
        result = heldout.generate_timed(model, tokenizer, prompt=prompt, max_new_tokens=512)
        return result
    prefix_ids, suffix_ids, prompt_audit = _rendered_parts(tokenizer, prompt)
    prefix_ids = prefix_ids.to("cuda:0")
    suffix_ids = suffix_ids.to("cuda:0")
    with torch.inference_mode():
        embed = model.get_input_embeddings()
        prefix = embed(prefix_ids)
        suffix = embed(suffix_ids)
        full = torch.cat((prefix, latent.to("cuda:0", dtype=prefix.dtype), suffix), dim=1)
    attention = torch.ones(full.shape[:2], dtype=torch.long, device="cuda:0")
    position_ids = torch.arange(full.shape[1], dtype=torch.long, device="cuda:0").unsqueeze(0)
    torch.cuda.synchronize()
    generation_started = time.monotonic()
    output = model.generate(
        inputs_embeds=full,
        attention_mask=attention,
        position_ids=position_ids,
        do_sample=False,
        temperature=None,
        top_p=None,
        top_k=None,
        max_new_tokens=512,
        use_cache=True,
        eos_token_id=model.generation_config.eos_token_id,
        pad_token_id=tokenizer.pad_token_id,
    )
    torch.cuda.synchronize()
    token_ids = output[0]
    wall = (time.monotonic() - generation_started) * 1000.0
    raw = tokenizer.decode(token_ids.detach().cpu().tolist(), skip_special_tokens=True).strip()
    eos_ids = model.generation_config.eos_token_id
    eos_set = {int(eos_ids)} if isinstance(eos_ids, int) else {int(item) for item in eos_ids or []}
    audit = layout_audit(
        prefix_tokens=int(prefix.shape[1]),
        latent_tokens=int(latent.shape[1]),
        suffix_tokens=int(suffix.shape[1]),
        response_tokens=max(1, int(token_ids.numel())),
    )
    return {
        "raw": raw,
        "token_ids": token_ids.detach().cpu().tolist(),
        "tokens": int(token_ids.numel()),
        "ttft_ms": wall,
        "generation_wall_ms": wall,
        "wall_ms": (time.monotonic() - started) * 1000.0,
        "eos": bool(eos_set & set(token_ids.detach().cpu().tolist())),
        "timeout": False,
        "layout": {**audit, **prompt_audit},
    }


def _score_record(record: Mapping[str, Any], generated: Mapping[str, Any], runner_id: str) -> dict[str, Any]:
    case = record["case"]
    evaluated = evaluate_solver_final(
        final_text=generated["raw"],
        case=case,
        trace=record["trace"],
        packet=record["packet"],
    )
    score = score_output(
        case,
        generated["raw"],
        evaluated.get("payload"),
        parser_error=evaluated.get("parse_error"),
        planner_rule_ids=record["planner"].get("rules_selected") or [],
        planner_source_ids=record["planner"].get("sources_selected") or [],
        runner_id=runner_id,
    )
    return {
        "case_id": case["id"],
        "runner_id": runner_id,
        "raw_hash": hashlib.sha256(generated["raw"].encode()).hexdigest(),
        "tokens": int(generated["tokens"]),
        "timings": {
            "ttft_ms": float(generated.get("ttft_ms") or generated.get("wall_ms") or 0),
            "generation_wall_ms": float(generated.get("generation_wall_ms") or generated.get("wall_ms") or 0),
            "wall_total_ms": float(generated.get("wall_ms") or generated.get("generation_wall_ms") or 0),
        },
        "gpu_peak_bytes": int(torch.cuda.max_memory_allocated()),
        "ram_peak_bytes": rss_bytes(),
        "timeout": bool(generated.get("timeout")),
        "parser": {key: value for key, value in evaluated.items() if key not in {"payload", "metadata"}},
        "payload": evaluated.get("payload"),
        "domain_opinion_v1": evaluated.get("payload"),
        "layout": generated.get("layout"),
        **score,
    }


def _aggregate_rows(rows: Sequence[Mapping[str, Any]], *, external: bool, cases: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    if external:
        enriched = [enrich_score(row, case, None) for row, case in zip(rows, cases)]
        metrics = aggregate_external(enriched, {str(case["id"]): case for case in cases})
    else:
        metrics = aggregate(rows)
        walls = sorted(float(row["timings"]["wall_total_ms"]) for row in rows)
        metrics["wall_median_ms"] = statistics.median(walls) if walls else 0.0
    metrics["format_errors"] = sum(row.get("error_classification") == "format_error" for row in rows)
    metrics["semantic_errors"] = sum(row.get("error_classification") == "solver_semantic_error" for row in rows)
    metrics["adapter_transfer_errors"] = sum(row.get("error_classification") == "adapter_transfer_error" for row in rows)
    metrics["solver_format_regressions"] = metrics["format_errors"]
    return metrics


def _evaluate(
    model: Any,
    tokenizer: Any,
    outer: OrderedOuter23 | None,
    records: Sequence[Mapping[str, Any]],
    *,
    split: str,
    cap: int | None,
    label: str,
    save_raw: bool = True,
) -> dict[str, Any]:
    rows = []
    if outer is not None:
        outer.eval().to("cuda:0", dtype=torch.float32)
    model.eval()
    torch.cuda.reset_peak_memory_stats()
    with torch.inference_mode():
        for record in records:
            latent = None
            if outer is not None and cap is not None:
                inner, meta = _load_cached(split, str(record["case"]["id"]))
                selected = ordered_cap(inner.to("cuda:0"), cap)
                latent = outer(selected)
                if meta["position_order"][:cap] != list(range(cap)):
                    raise RuntimeError("h2_latent_order_changed")
            generated = _generate(model, tokenizer, prompt=record["prompt"], latent=latent)
            row = _score_record(record, generated, label)
            rows.append(row)
            if save_raw:
                case_root = ROOT / "evaluations" / label / str(record["case"]["id"])
                case_root.mkdir(parents=True, exist_ok=True)
                (case_root / "raw_sanitized.txt").write_text(generated["raw"] + "\n", encoding="utf-8")
                dump(case_root / "result.json", {key: value for key, value in row.items() if key != "payload"})
    metrics = _aggregate_rows(rows, external=split in {"final_a", "reserve_b"}, cases=[row["case"] for row in records])
    metrics.update(
        {
            "label": label,
            "cap": cap,
            "gpu_peak_bytes": int(torch.cuda.max_memory_allocated()),
            "ram_peak_bytes": rss_bytes(),
            "solver_inner_called": False,
            "outer31_present": False,
            "pooling": None,
        }
    )
    dump(ROOT / "evaluations" / label / "summary.json", metrics)
    return metrics


def _save_outer(module: nn.Module, path: Path, metadata: Mapping[str, Any]) -> dict[str, Any]:
    if any(parameter.dtype != torch.float32 for parameter in module.parameters()):
        raise ValueError("h2_outer23_not_fp32")
    path.parent.mkdir(parents=True, exist_ok=True)
    torch.save(
        {"state_dict": {name: value.detach().cpu() for name, value in module.state_dict().items()}, "metadata": dict(metadata)},
        path,
    )
    record = {
        **dict(metadata),
        "path": str(path),
        "sha256": sha256_file(path),
        **architecture_manifest(module),
    }
    dump(path.with_suffix(".json"), record)
    return record


def _load_outer(record: Mapping[str, Any]) -> OrderedOuter23:
    path = Path(record["path"])
    if sha256_file(path) != record["sha256"]:
        raise RuntimeError("h2_outer23_checkpoint_hash_mismatch")
    module = initialize_outer23(int(record["cap"]))
    payload = torch.load(path, map_location="cpu", weights_only=True)
    module.load_state_dict(payload["state_dict"], strict=True)
    return module


def _ce_step(
    model: Any,
    tokenizer: Any,
    outer: OrderedOuter23,
    record: Mapping[str, Any],
    *,
    split: str,
    cap: int,
) -> tuple[torch.Tensor, dict[str, Any]]:
    inner, meta = _load_cached(split, str(record["case"]["id"]))
    source = ordered_cap(inner.to("cuda:0", dtype=torch.float32), cap)
    latent = outer(source)
    full, labels, position_ids, audit = _training_input(
        model,
        tokenizer,
        prompt=record["prompt"],
        target=record["target"],
        latent=latent,
    )
    attention = torch.ones(full.shape[:2], dtype=torch.long, device="cuda:0")
    with torch.autocast(device_type="cuda", dtype=torch.float16):
        output = model.model(
            inputs_embeds=full,
            attention_mask=attention,
            position_ids=position_ids,
            use_cache=False,
            return_dict=True,
        )
        shifted = labels[:, 1:]
        positions = shifted != -100
        hidden = output.last_hidden_state[:, :-1, :][positions]
        targets = shifted[positions]
        logits = model.lm_head(hidden).float()
        loss = functional.cross_entropy(logits, targets)
    audit.update(
        {
            "case_id": record["case"]["id"],
            "sequence_before_cap": meta["sequence_before_cap"],
            "sequence_after_cap": cap,
            "first_positions_retained": meta["position_order"][:cap] == list(range(cap)),
            "final_token_ce": True,
        }
    )
    del output, shifted, positions, hidden, targets, logits, full, labels, position_ids
    return loss, audit


def _gradient_norm(module: nn.Module) -> float:
    return math.sqrt(
        sum(float(parameter.grad.detach().float().square().sum()) for parameter in module.parameters() if parameter.grad is not None)
    )


def _train(cap: int, *, micro: bool) -> dict[str, Any]:
    config = H2Config()
    split = "micro" if micro else "train"
    records = _split_records(split)
    validation = records if micro else _split_records("validation")
    outer = initialize_outer23(cap).to("cuda:0", dtype=torch.float32)
    optimizer = torch.optim.AdamW(outer.parameters(), lr=config.learning_rate, betas=(0.9, 0.95))
    model, tokenizer, load = _model(QWEN3, training=True)
    max_steps = config.micro_steps if micro else config.max_steps
    evaluate_every = 10 if micro else config.evaluate_every
    best: dict[str, Any] | None = None
    logs = []
    stale = 0
    blocked = None
    torch.cuda.reset_peak_memory_stats()
    try:
        for step in range(1, max_steps + 1):
            started = time.monotonic()
            optimizer.zero_grad(set_to_none=True)
            total = 0.0
            mask_audit = None
            for offset in range(config.gradient_accumulation):
                record = records[((step - 1) * config.gradient_accumulation + offset) % len(records)]
                loss, mask_audit = _ce_step(model, tokenizer, outer, record, split=split, cap=cap)
                (loss / config.gradient_accumulation).backward()
                total += float(loss.detach())
            gradient = _gradient_norm(outer)
            if not math.isfinite(gradient) or any(not torch.isfinite(parameter).all() for parameter in outer.parameters()):
                blocked = "NaN_or_Inf"
                break
            torch.nn.utils.clip_grad_norm_(outer.parameters(), config.gradient_clip)
            optimizer.step()
            row = {
                "step": step,
                "final_token_ce": total / config.gradient_accumulation,
                "outer23_gradient_norm": gradient,
                "base_trainable_parameters": load["trainable_parameters"],
                "mask": mask_audit,
                "gpu_allocated_bytes": int(torch.cuda.memory_allocated()),
                "gpu_reserved_bytes": int(torch.cuda.memory_reserved()),
                "ram_peak_bytes": rss_bytes(),
                "step_ms": (time.monotonic() - started) * 1000.0,
            }
            logs.append(row)
            if step % evaluate_every == 0:
                model.gradient_checkpointing_disable()
                metrics = _evaluate(
                    model,
                    tokenizer,
                    outer,
                    validation,
                    split=split if micro else "validation",
                    cap=cap,
                    label=f"cap{cap}_{'micro' if micro else 'validation'}_step_{step:03d}",
                )
                model.gradient_checkpointing_enable(gradient_checkpointing_kwargs={"use_reentrant": False})
                metrics.update(
                    {
                        "outer23_gradient_norm": gradient,
                        "base_trainable_parameters": load["trainable_parameters"],
                        "nan_or_inf": 0,
                        "oom": False,
                    }
                )
                checkpoint = _save_outer(
                    outer,
                    ROOT / f"cap{cap}" / ("micro" if micro else "checkpoints") / f"step_{step:03d}.pt",
                    {"cap": cap, "step": step, "micro": micro, "validation_metrics": metrics},
                )
                candidate = {"checkpoint": checkpoint, "metrics": metrics, "step": step}
                row["evaluation"] = metrics
                if micro:
                    gate = micro_gate(metrics)
                    row["gate"] = gate
                    if gate["passed"]:
                        best = candidate
                        break
                elif best is None or selection_key(metrics) > selection_key(best["metrics"]):
                    best = candidate
                    stale = 0
                else:
                    stale += 1
                    if stale >= config.validation_patience:
                        break
                outer.train().to("cuda:0", dtype=torch.float32)
                model.eval()
                print(
                    json.dumps(
                        {
                            "cap": cap,
                            "micro": micro,
                            "step": step,
                            "valid": metrics["semantic_complete_count"],
                            "schema": metrics["schema_valid_count"],
                            "best": best["step"] if best else None,
                        }
                    ),
                    flush=True,
                )
    except torch.cuda.OutOfMemoryError:
        blocked = "OOM"
        optimizer.zero_grad(set_to_none=True)
        torch.cuda.empty_cache()
    finally:
        outer.to("cpu")
        _unload(model, tokenizer)
    result = {
        "cap": cap,
        "micro": micro,
        "steps": len(logs),
        "loss_initial": logs[0]["final_token_ce"] if logs else None,
        "loss_final": logs[-1]["final_token_ce"] if logs else None,
        "blocked": blocked,
        "best": best,
        "logs": logs,
        "gpu_peak_bytes": max((row["gpu_reserved_bytes"] for row in logs), default=0),
        "ram_peak_bytes": max((row["ram_peak_bytes"] for row in logs), default=rss_bytes()),
        "base_model_frozen": load["trainable_parameters"] == 0,
        "solver_inner_called": False,
        "outer31_present": False,
        "pooling": None,
    }
    if micro:
        result["gate"] = micro_gate(best["metrics"] if best else {
            "outer23_gradient_norm": logs[-1]["outer23_gradient_norm"] if logs else 0,
            "base_trainable_parameters": load["trainable_parameters"],
            "oom": blocked == "OOM",
            "nan_or_inf": blocked == "NaN_or_Inf",
        })
    dump(ROOT / "evaluations" / f"cap{cap}_{'micro' if micro else 'training'}.json", result)
    return result


def prepare() -> dict[str, Any]:
    config = H2Config()
    config.validate()
    for name in ("manifests", "cached_trajectories", "cap16", "cap32", "evaluations", "final", "reserve_b"):
        (ROOT / name).mkdir(parents=True, exist_ok=True)
    if sha256_file(H1B_CHECKPOINT) != H1B_SHA256:
        raise RuntimeError("h2_critic_inner_sha_mismatch")
    if sha256_file(H1B_FREEZE) != H1B_FREEZE_SHA256:
        raise RuntimeError("h2_h1b_freeze_sha_mismatch")
    invariants = h1.verify_invariants()
    reserve = _reserve_metadata()
    if reserve != {
        **reserve,
        "sha256": RESERVE_SHA256,
        "records": 24,
        "permissions": "0o600",
    }:
        if reserve["sha256"] != RESERVE_SHA256 or reserve["records"] != 24 or reserve["permissions"] != "0o600":
            raise RuntimeError("h2_reserve_b_seal_invalid")
    train_manifest = json.loads((_source_cache("train") / "manifest.json").read_text())
    validation_manifest = json.loads((_source_cache("validation") / "manifest.json").read_text())
    value = {
        **lab_manifest(config),
        "git_commit": git_head(),
        "critic_inner_h1b": {"path": str(H1B_CHECKPOINT), "sha256": H1B_SHA256, "frozen": True},
        "h1b_freeze_sha256": H1B_FREEZE_SHA256,
        "train_real": {"records": 168, "corpus_sha256": train_manifest["source_corpus_sha256"]},
        "validation_real": {"records": 36, "corpus_sha256": validation_manifest["source_corpus_sha256"]},
        "critic_revision": h1.CRITIC_REVISION,
        "solver_revision": old_micro.MODEL_SPECS["solver"]["revision"],
        "critic_tokenizer_hash": h1.tokenizer_hash(h1.CRITIC_SNAPSHOT),
        "solver_tokenizer_hash": json.loads(RUNNER_FREEZE.read_text())["tokenizer_hashes"]["solver"],
        "prompt_hash": sha256_file(REPO / "ralfloop_agent/domains/recursive_mas_domain_provenance.py"),
        "evidence_packet_hash": sha256_file(REPO / "ralfloop_agent/domains/recursive_mas_domain_provenance.py"),
        "parser_hash": sha256_file(REPO / "ralfloop_agent/domains/recursive_mas_qwen3_solver_eval.py"),
        "serializer_hash": sha256_file(REPO / "ralfloop_agent/domains/recursive_mas_domain_serialization.py"),
        "generation_config": GENERATION_CONFIG,
        "feature_flag": invariants["feature_flag"],
        "math_profile_sha256": invariants["math_profile_sha256"],
        "telegram_gate_sha256": invariants["telegram_gate_sha256"],
        "reserve_b": reserve,
        "graph": [
            "critic_hidden_assistant_trajectory",
            "critic_inner_h1b_frozen",
            "first_N_ordered_positions",
            "new_outer23",
            "Qwen3_inputs_embeds",
            "response_only_final_CE",
        ],
        "solver_inner_called": False,
        "outer31_present": False,
        "pooling": None,
    }
    dump(ROOT / "manifests/lab_manifest.json", value)
    _cache_h1b_split("train")
    _cache_h1b_split("validation")
    _cache_micro_real()
    return value


def micro() -> dict[str, Any]:
    if not (ROOT / "manifests/lab_manifest.json").exists():
        raise RuntimeError("h2_prepare_required")
    results = {f"cap{cap}": _train(cap, micro=True) for cap in ALLOWED_CAPS}
    dump(ROOT / "evaluations/micro_summary.json", results)
    return results


def train() -> dict[str, Any]:
    micro_summary = json.loads((ROOT / "evaluations/micro_summary.json").read_text())
    results = {}
    for cap in ALLOWED_CAPS:
        if micro_summary[f"cap{cap}"]["gate"]["passed"]:
            results[f"cap{cap}"] = _train(cap, micro=False)
        else:
            results[f"cap{cap}"] = {"cap": cap, "admitted": False, "reason": "micro_gate_failed", "best": None}
    dump(ROOT / "evaluations/training_summary.json", results)
    return results


def _historical_direct_baseline() -> dict[str, Any]:
    freeze = json.loads(RUNNER_FREEZE.read_text())
    verify_runner_freeze(REPO, freeze)
    records = _split_records("final_a")
    rows = []
    external_upstream = json.loads((EXTERNAL_ROOT / "upstream.json").read_text())["cases"]
    h1b_upstream = json.loads(_upstream_path("final_a").read_text())["cases"]
    for record in records:
        case_id = str(record["case"]["id"])
        if external_upstream[case_id]["critic"]["raw_sha256"] != h1b_upstream[case_id]["critic"]["raw_sha256"]:
            raise RuntimeError("h2_historical_baseline_critic_mismatch")
        path = EXTERNAL_ROOT / "runs/B_qwen3_direct" / f"{case_id}.json"
        row = json.loads(path.read_text())
        if row["input_hash"] != stable_sha256(input_payload(record["case"])):
            raise RuntimeError("h2_historical_baseline_case_hash_mismatch")
        rows.append(row)
    metrics = aggregate_external(rows, {str(record["case"]["id"]): record["case"] for record in records})
    metrics["format_errors"] = sum(row.get("error_classification") == "format_error" for row in rows)
    metrics["semantic_errors"] = sum(row.get("error_classification") == "solver_semantic_error" for row in rows)
    metrics["adapter_transfer_errors"] = 0
    metrics["solver_format_regressions"] = 0
    metrics["source"] = "historical_verified_exact"
    metrics["runner_freeze_sha256"] = sha256_file(RUNNER_FREEZE)
    dump(ROOT / "evaluations/qwen3_direct_final_a.json", metrics)
    return metrics


def freeze() -> dict[str, Any]:
    training = json.loads((ROOT / "evaluations/training_summary.json").read_text())
    baseline = _historical_direct_baseline()
    output = {"baseline": baseline, "caps": {}}
    for cap in ALLOWED_CAPS:
        best = training[f"cap{cap}"].get("best")
        if best is None:
            value = {"cap": cap, "eligible": False, "reason": "no_validation_checkpoint", "final_a_executed": False}
        else:
            checkpoint = best["checkpoint"]
            value = {
                "profile_id": "recursive_mas_domain_h2_outer23_v1",
                "cap": cap,
                "eligible": True,
                "checkpoint": checkpoint,
                "checkpoint_sha256": checkpoint["sha256"],
                "architecture_hash": checkpoint["architecture_sha256"],
                "train_corpus_hash": json.loads((ROOT / "cached_trajectories/train/manifest.json").read_text())["source_corpus_sha256"],
                "validation_corpus_hash": json.loads((ROOT / "cached_trajectories/validation/manifest.json").read_text())["source_corpus_sha256"],
                "critic_inner_sha256": H1B_SHA256,
                "critic_revision": h1.CRITIC_REVISION,
                "solver_revision": old_micro.MODEL_SPECS["solver"]["revision"],
                "tokenizer_hash": json.loads(RUNNER_FREEZE.read_text())["tokenizer_hashes"]["solver"],
                "prompt_hash": sha256_file(REPO / "ralfloop_agent/domains/recursive_mas_domain_provenance.py"),
                "packet_hash": sha256_file(REPO / "ralfloop_agent/domains/recursive_mas_domain_provenance.py"),
                "parser_hash": sha256_file(REPO / "ralfloop_agent/domains/recursive_mas_qwen3_solver_eval.py"),
                "serializer_hash": sha256_file(REPO / "ralfloop_agent/domains/recursive_mas_domain_serialization.py"),
                "decoding_config": GENERATION_CONFIG,
                "validation_metrics": best["metrics"],
                "git_commit": git_head(),
                "final_a_executed": False,
                "solver_inner_called": False,
                "outer31_present": False,
                "pooling": None,
            }
        path = ROOT / "manifests" / f"h2_cap{cap}_frozen.json"
        dump(path, value)
        value["manifest_sha256"] = sha256_file(path)
        output["caps"][f"cap{cap}"] = value
    dump(ROOT / "evaluations/freeze_summary.json", output)
    return output


def final_a() -> dict[str, Any]:
    freezes = {
        cap: json.loads((ROOT / "manifests" / f"h2_cap{cap}_frozen.json").read_text()) for cap in ALLOWED_CAPS
    }
    if any(value.get("final_a_executed") is not False for value in freezes.values()):
        raise RuntimeError("h2_final_a_already_executed")
    _cache_h1b_split("final_a")
    records = _split_records("final_a")
    baseline = json.loads((ROOT / "evaluations/qwen3_direct_final_a.json").read_text())
    model, tokenizer, load = _model(QWEN3, training=False)
    results = {}
    try:
        for cap in ALLOWED_CAPS:
            frozen = freezes[cap]
            if not frozen.get("eligible"):
                results[f"cap{cap}"] = {"cap": cap, "executed": False, "reason": frozen["reason"], "gate": {"passed": False}}
                continue
            outer = _load_outer(frozen["checkpoint"])
            metrics = _evaluate(
                model,
                tokenizer,
                outer,
                records,
                split="final_a",
                cap=cap,
                label=f"cap{cap}_final_a",
            )
            gate = h2_gate(metrics, baseline)
            results[f"cap{cap}"] = {"cap": cap, "executed": True, "metrics": metrics, "gate": gate}
            outer.to("cpu")
    finally:
        _unload(model, tokenizer)
    passed = [value for value in results.values() if value.get("gate", {}).get("passed")]
    best = max(passed, key=lambda value: selection_key(value["metrics"])) if passed else None
    summary = {
        "baseline": baseline,
        "caps": results,
        "h2_passed": best is not None,
        "best": best,
        "classification": "recursive_domain_h2_passed" if best else "recursive_domain_h2_failed",
        "branch_closed": best is None,
        "reserve_b_authorized": best is not None,
        "solver_load": load,
    }
    dump(ROOT / "evaluations/final_a_summary.json", summary)
    if best:
        cap = int(best["cap"])
        frozen = freezes[cap]
        candidate = {
            "profile_id": "recursive_mas_domain_h2_outer23_v1",
            "cap": cap,
            "checkpoint": frozen["checkpoint"],
            "checkpoint_sha256": frozen["checkpoint_sha256"],
            "validation_metrics": frozen["validation_metrics"],
            "final_a_metrics": best["metrics"],
            "decision_rule": best["gate"],
            "feature_enabled": False,
            "reserve_b_executed": False,
        }
        dump(ROOT / "final/h2_candidate_manifest.json", candidate)
    return summary


def _external_upstream_and_cache(cases: Sequence[Mapping[str, Any]], split: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if split != "reserve_b":
        raise ValueError("h2_external_cache_split_invalid")
    planner_model, planner_tokenizer, planner_load = _model(old_micro.QWEN25_3B, training=False)
    upstream: dict[str, Any] = {"cases": {}}
    try:
        for case in cases:
            prompt = heldout.planner_prompt(case)
            generated = heldout.generate_timed(planner_model, planner_tokenizer, prompt=prompt, max_new_tokens=512)
            structured = heldout._filter_planner(case, heldout._json_object(generated["raw"]))
            upstream["cases"][str(case["id"])] = {"planner": {**generated, "structured_output": structured}}
    finally:
        _unload(planner_model, planner_tokenizer)
    critic_model, critic_tokenizer, critic_load = _model(QWEN25_15B, training=False)
    inner = _load_h1b().to("cuda:0")
    cache_rows = []
    records = []
    try:
        embedding = critic_model.get_input_embeddings()
        for case in cases:
            case_id = str(case["id"])
            planner = upstream["cases"][case_id]["planner"]["structured_output"]
            prompt = heldout.critic_prompt(case, planner)
            generated = heldout.generate_timed(critic_model, critic_tokenizer, prompt=prompt, max_new_tokens=512)
            critic = heldout._json_object(generated["raw"]) or {}
            raw_ids = [int(value) for value in generated["token_ids"]]
            prompt_ids = heldout._chat_ids(critic_tokenizer, prompt)
            tensors = full_raw_teacher_forcing(
                prompt_ids=prompt_ids,
                raw_output_ids=raw_ids,
                pad_token_id=int(critic_tokenizer.pad_token_id),
            )
            ids = tensors["full_ids"].to("cuda:0")
            attention = tensors["attention_mask"].to("cuda:0")
            assistant = tensors["assistant_mask"].to("cuda:0")
            with torch.inference_mode():
                result = critic_model(
                    input_ids=ids,
                    attention_mask=attention,
                    output_hidden_states=True,
                    use_cache=False,
                    return_dict=True,
                )
                pairs = build_positionwise_pairs(
                    hidden_states=result.hidden_states[-1],
                    full_ids=ids,
                    input_embeddings=embedding,
                    attention_mask=attention,
                    assistant_mask=assistant,
                )
                source = pairs["source_hidden"][pairs["pair_mask"]].float().unsqueeze(0)
                inner_value = inner(source).cpu()
            path = ROOT / "cached_trajectories/reserve_b" / f"{case_id}.pt"
            path.parent.mkdir(parents=True, exist_ok=True)
            torch.save({"critic_hidden": source.cpu(), "critic_inner_h1b": inner_value}, path)
            meta = {
                "case_id": case_id,
                "raw_output_hash": hashlib.sha256(generated["raw"].encode()).hexdigest(),
                "source_hidden_hash": tensor_sha256(source),
                "critic_inner_output_hash": tensor_sha256(inner_value),
                "sequence_before_cap": int(inner_value.shape[1]),
                "position_order": list(range(int(inner_value.shape[1]))),
                "pooling": None,
                "path": str(path),
            }
            dump(path.with_suffix(".json"), meta)
            cache_rows.append(meta)
            packet = build_real_packet(case, planner, critic)
            records.append(
                {
                    "case": case,
                    "trace": {"case_id": case_id},
                    "critic": critic,
                    "planner": planner,
                    "packet": packet,
                    "prompt": heldout.solver_prompt(case, critic, packet),
                }
            )
            upstream["cases"][case_id]["critic"] = {**generated, "structured_output": critic}
            del result, pairs, ids, attention, assistant
    finally:
        inner.to("cpu")
        _unload(critic_model, critic_tokenizer)
    dump(ROOT / "reserve_b/upstream.json", upstream)
    dump(
        ROOT / "cached_trajectories/reserve_b/manifest.json",
        {"records": len(cache_rows), "trajectory_hash": stable_sha256([row["critic_inner_output_hash"] for row in cache_rows])},
    )
    return records, {"planner_load": planner_load, "critic_load": critic_load}


def reserve_b() -> dict[str, Any]:
    final_summary = json.loads((ROOT / "evaluations/final_a_summary.json").read_text())
    if not final_summary.get("h2_passed"):
        value = {
            "executed": False,
            "execution_count": 0,
            "reason": "h2_failed",
            "classification": "recursive_domain_branch_closed",
        }
        dump(ROOT / "reserve_b/summary.json", value)
        return value
    candidate = json.loads((ROOT / "final/h2_candidate_manifest.json").read_text())
    if candidate.get("reserve_b_executed") is not False or (ROOT / "reserve_b/execution_started.json").exists():
        raise RuntimeError("h2_reserve_b_execution_count_exceeded")
    metadata = _reserve_metadata()
    if metadata["sha256"] != RESERVE_SHA256 or metadata["records"] != 24 or metadata["permissions"] != "0o600":
        raise RuntimeError("h2_reserve_b_seal_invalid")
    dump(ROOT / "reserve_b/execution_started.json", {"execution_count_before": 0, "execution_count": 1, "started_at": time.time()})
    cases = load_external_cases(RESERVE_B, allow_reserve=True)
    records, upstream_resources = _external_upstream_and_cache(cases, "reserve_b")
    cap = int(candidate["cap"])
    outer = _load_outer(candidate["checkpoint"])
    model, tokenizer, load = _model(QWEN3, training=False)
    try:
        direct = _evaluate(model, tokenizer, None, records, split="reserve_b", cap=None, label="reserve_b_direct")
        h2_metrics = _evaluate(model, tokenizer, outer, records, split="reserve_b", cap=cap, label="reserve_b_h2")
    finally:
        outer.to("cpu")
        _unload(model, tokenizer)
    gate = reserve_gate(h2_metrics, direct)
    value = {
        "executed": True,
        "execution_count_before": 0,
        "execution_count": 1,
        "direct": direct,
        "h2": h2_metrics,
        "gate": gate,
        "classification": gate["classification"] if gate["passed"] else "recursive_domain_reserve_b_failed",
        "branch_closed": not gate["passed"],
        "candidate_for_integration": gate["passed"],
        "resources": {"upstream": upstream_resources, "solver": load},
    }
    dump(ROOT / "reserve_b/summary.json", value)
    candidate["reserve_b_executed"] = True
    candidate["reserve_b_execution_count"] = 1
    candidate["reserve_b_gate"] = gate
    dump(ROOT / "final/h2_candidate_manifest.json", candidate)
    if gate["passed"]:
        profile = {
            "profile_id": "recursive_mas_domain_h2_v1",
            "enabled": False,
            "registered": False,
            "feature_flag": "RALF_RECURSIVE_DOMAIN_REASONING=0",
            "critic_inner_checkpoint": str(H1B_CHECKPOINT),
            "critic_inner_sha256": H1B_SHA256,
            "outer23_checkpoint": candidate["checkpoint"]["path"],
            "outer23_sha256": candidate["checkpoint_sha256"],
            "latent_cap": cap,
            "solver_inner": "bypass",
            "outer31": "absent",
            "pooling": None,
        }
        dump(REPO / "ralfloop_agent/domains/recursive_mas_domain_h2_v1.json", profile)
    return value


def finalize() -> dict[str, Any]:
    final = json.loads((ROOT / "evaluations/final_a_summary.json").read_text())
    reserve_path = ROOT / "reserve_b/summary.json"
    reserve = json.loads(reserve_path.read_text()) if reserve_path.exists() else {
        "executed": False,
        "execution_count": 0,
        "classification": "not_authorized",
    }
    invariants = h1.verify_invariants()
    passed = bool(reserve.get("candidate_for_integration"))
    value = {
        "h2": final,
        "reserve_b": reserve,
        "classification": (
            "recursive_qwen3_domain_candidate_for_integration"
            if passed
            else "recursive_domain_branch_closed"
        ),
        "candidate_for_integration": passed,
        "production_authorized": False,
        "additional_training_authorized": False,
        "feature_flag": invariants["feature_flag"],
        "math_profile_sha256": invariants["math_profile_sha256"],
        "telegram_gate_sha256": invariants["telegram_gate_sha256"],
        "approval_created_or_executed": False,
        "services_restarted": False,
        "production_modified": False,
        "models_downloaded": False,
        "solver_inner_called": False,
        "outer31_present": False,
        "pooling": None,
    }
    dump(ROOT / "final/summary.json", value)
    return value


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "phase",
        choices=("prepare", "micro", "train", "freeze", "final-a", "reserve-b", "finalize", "all"),
    )
    args = parser.parse_args()
    if args.phase == "prepare":
        result = prepare()
    elif args.phase == "micro":
        result = micro()
    elif args.phase == "train":
        result = train()
    elif args.phase == "freeze":
        result = freeze()
    elif args.phase == "final-a":
        result = final_a()
    elif args.phase == "reserve-b":
        result = reserve_b()
    elif args.phase == "finalize":
        result = finalize()
    else:
        prepare()
        micro_result = micro()
        if any(value["gate"]["passed"] for value in micro_result.values()):
            train()
            freeze()
            final_result = final_a()
            if final_result["h2_passed"]:
                reserve_b()
            else:
                reserve_b()
            result = finalize()
        else:
            train()
            freeze()
            result = {
                "classification": "recursive_domain_h2_failed",
                "branch_closed": True,
                "reserve_b_executed": False,
            }
            dump(ROOT / "final/summary.json", result)
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
