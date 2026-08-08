from __future__ import annotations

import argparse
from collections.abc import Mapping
import gc
import hashlib
import json
import math
from pathlib import Path
import resource
import time
from typing import Any

import torch
import torch.nn.functional as functional
from torch import nn
from transformers import AutoModelForCausalLM, AutoTokenizer

from ralfloop_agent.domains.recursive_mas_domain_dataset import generate_dataset
from ralfloop_agent.domains.recursive_mas_domain_instruct_profile import tokenizer_sha256
from ralfloop_agent.domains.recursive_mas_domain_prompts import (
    PLANNER_SLOT,
    build_domain_critic_prompt_with_slot,
    build_domain_planner_prompt,
)
from ralfloop_agent.domains.recursive_mas_domain_provenance import build_provenance_solver_prompt
from ralfloop_agent.domains.recursive_mas_domain_serialization import reference_accuracy
from ralfloop_agent.domains.recursive_mas_qwen3_micro_overfit import (
    ARTIFACT_ROOT,
    FIXTURE_HASH,
    MODEL_SPECS,
    NativeMicroConfig,
    adapter_gradient_report,
    adapter_manifest,
    initialize_native_adapters,
    load_adapter_checkpoint,
    load_hidden_cache,
    micro_manifest,
    native_graph,
    response_only_loss_labels,
    save_adapter_checkpoint,
    save_hidden_cache,
    stage_a_gate,
    stage_b_gate,
    stage_c_gate,
)
from ralfloop_agent.domains.recursive_mas_qwen3_solver_eval import (
    CANARY_CASE_IDS,
    aggregate_solver_results,
    build_gold_packet,
    build_real_packet,
    classify_pipeline_error,
    evaluate_solver_final,
    fixture_hash_record,
    stable_sha256,
)


REPO = Path("/home/sibilla-cumana/ralfloop_agent_scaffold")
ROOT = REPO / ARTIFACT_ROOT
QWEN25_3B = Path("/home/sibilla-cumana/RecursiveMAS/.hf-domain-instruct-v1/hub/models--Qwen--Qwen2.5-3B-Instruct/snapshots/aa8e72537993ba99e69dfaafa59ed015b17504d1")
QWEN25_15B = Path("/home/sibilla-cumana/RecursiveMAS/.hf-domain-instruct-v1/hub/models--Qwen--Qwen2.5-1.5B-Instruct/snapshots/989aa7980e4cf806f80c7fef2b1adb7bc71aa306")
QWEN3 = Path("/home/sibilla-cumana/RecursiveMAS/.hf-domain-qwen3-solver-v1/hub/models--Qwen--Qwen3-1.7B/snapshots/70d244cc86ccca08cf5af4e1e306ecf908b1ad5e")
UPSTREAM = REPO / ".ralf_run/recursive_domain_instruct_v1/direct_text_canary/summary.json"
DIRECT_TARGETS = REPO / ".ralf_run/recursive_domain_qwen3_solver/protocol_canary/B_newline_prompt_strict"
GOLD_PACKETS = REPO / ".ralf_run/recursive_domain_qwen3_solver/gold_evidence_packets.json"
REAL_PACKET_MANIFEST = REPO / ".ralf_run/recursive_domain_qwen3_solver/real_packet_manifest.json"
PROTOCOL_SUMMARY = REPO / ".ralf_run/recursive_domain_qwen3_solver/protocol_canary/summary.json"
STRICT_PROVENANCE_SUFFIX = (
    "\nSTRICT PROVENANCE OUTPUT: Never copy any literal identifier beginning with R_ or S_. "
    "Describe evidence meaning only; provenance remains exclusively in the upstream packet."
)


def dump(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def domain_payload(case: Mapping[str, Any]) -> str:
    keys = (
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
    return json.dumps({key: case[key] for key in keys}, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def chat_ids(tokenizer: Any, prompt: str, assistant: str | None = None) -> list[int]:
    messages = [{"role": "user", "content": prompt}]
    if assistant is not None:
        messages.append({"role": "assistant", "content": assistant})
    value = tokenizer.apply_chat_template(
        messages,
        tokenize=True,
        add_generation_prompt=assistant is None,
        enable_thinking=False,
    )
    if isinstance(value, Mapping):
        value = value["input_ids"]
    if hasattr(value, "tolist"):
        value = value.tolist()
    if value and isinstance(value[0], list):
        value = value[0]
    return [int(item) for item in value]


def context() -> dict[str, Any]:
    cases_raw, traces_raw = generate_dataset()
    cases = {item["id"]: item for item in cases_raw}
    traces = {item["case_id"]: item for item in traces_raw}
    upstream = json.loads(UPSTREAM.read_text(encoding="utf-8"))
    if upstream["case_ids"] != list(CANARY_CASE_IDS):
        raise RuntimeError("qwen3_micro_case_order_mismatch")
    gold_packets = {case_id: build_gold_packet(cases[case_id], traces[case_id]) for case_id in CANARY_CASE_IDS}
    real_packets = {}
    for case_id in CANARY_CASE_IDS:
        stages = upstream["cases"][case_id]
        real_packets[case_id] = build_real_packet(
            cases[case_id],
            stages["planner"]["structured_output"] or {},
            stages["critic"]["structured_output"] or {},
        )
    targets = {case_id: (DIRECT_TARGETS / case_id / "raw_sanitized.txt").read_text(encoding="utf-8").strip() for case_id in CANARY_CASE_IDS}
    return {
        "cases": cases,
        "traces": traces,
        "upstream": upstream,
        "gold_packets": gold_packets,
        "real_packets": real_packets,
        "targets": targets,
    }


def solver_prompt(case: Mapping[str, Any], critic: Mapping[str, Any], packet: Any) -> str:
    return build_provenance_solver_prompt(
        case,
        critic,
        packet,
        request_slots=False,
        demo_mode="none",
        strict_newlines=True,
    ) + STRICT_PROVENANCE_SUFFIX


def select_target_length(tokenizer: Any, targets: Mapping[str, str], config: NativeMicroConfig) -> dict[str, Any]:
    lengths = {
        case_id: len(tokenizer(text, add_special_tokens=False)["input_ids"]) + 1
        for case_id, text in targets.items()
    }
    attempted = []
    selected = None
    for length in config.target_sequence_lengths:
        attempted.append(length)
        if max(lengths.values()) <= length:
            selected = length
            break
    if selected is None and max(lengths.values()) <= config.max_target_sequence_length:
        selected = config.max_target_sequence_length
        attempted.append(selected)
    if selected is None:
        raise RuntimeError("canonical_target_exceeds_512")
    return {"per_case": lengths, "attempted": attempted, "selected": selected, "max": max(lengths.values())}


def _load_model(snapshot: Path, *, train_solver: bool = False) -> tuple[Any, Any, dict[str, Any]]:
    load_started = time.monotonic()
    tokenizer = AutoTokenizer.from_pretrained(snapshot, local_files_only=True, trust_remote_code=True, use_fast=True)
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
    if train_solver:
        model.gradient_checkpointing_enable(gradient_checkpointing_kwargs={"use_reentrant": False})
        model.config.use_cache = False
        model.train()
    return model, tokenizer, {
        "load_ms": (time.monotonic() - load_started) * 1000.0,
        "parameters": sum(parameter.numel() for parameter in model.parameters()),
        "trainable_parameters": sum(parameter.numel() for parameter in model.parameters() if parameter.requires_grad),
        "device": str(next(model.parameters()).device),
        "dtype": str(next(model.parameters()).dtype),
    }


def _unload(model: Any, tokenizer: Any) -> None:
    model.to("cpu")
    del model, tokenizer
    gc.collect()
    torch.cuda.empty_cache()
    torch.cuda.synchronize()


def extract_assistant_hidden(
    model: Any,
    tokenizer: Any,
    *,
    prompt: str,
    assistant: str,
) -> tuple[torch.Tensor, torch.Tensor, dict[str, Any]]:
    prompt_ids = chat_ids(tokenizer, prompt)
    full_ids = chat_ids(tokenizer, prompt, assistant)
    if len(full_ids) <= len(prompt_ids):
        raise RuntimeError("hidden_cache_assistant_span_missing")
    ids = torch.tensor(full_ids, device="cuda:0", dtype=torch.long).unsqueeze(0)
    attention = torch.ones_like(ids)
    with torch.inference_mode():
        result = model(
            input_ids=ids,
            attention_mask=attention,
            output_hidden_states=True,
            use_cache=False,
            return_dict=True,
        )
    response_end = len(full_ids) - 1
    response_start = min(len(prompt_ids), response_end - 1)
    response = result.hidden_states[-1][:, response_start:response_end, :].float()
    pooled = response.mean(dim=1, keepdim=True).cpu()
    mask = torch.ones((1, 1), dtype=torch.long)
    return pooled, mask, {
        "source_sequence_length": len(full_ids),
        "assistant_sequence_length": int(response.shape[1]),
        "pooled_sequence_length": 1,
        "pooling": "masked_mean_assistant_hidden",
        "input_hash": stable_sha256({"prompt": prompt, "assistant": assistant}),
    }


def extract_prompt_hidden(model: Any, tokenizer: Any, *, prompt: str) -> tuple[torch.Tensor, torch.Tensor, dict[str, Any]]:
    ids_list = chat_ids(tokenizer, prompt)
    ids = torch.tensor(ids_list, device="cuda:0", dtype=torch.long).unsqueeze(0)
    attention = torch.ones_like(ids)
    with torch.inference_mode():
        result = model(
            input_ids=ids,
            attention_mask=attention,
            output_hidden_states=True,
            use_cache=False,
            return_dict=True,
        )
    hidden = result.hidden_states[-1][:, -1:, :].float().cpu()
    return hidden, torch.ones((1, 1), dtype=torch.long), {
        "source_sequence_length": len(ids_list),
        "pooled_sequence_length": 1,
        "pooling": "last_prompt_hidden",
        "input_hash": stable_sha256(prompt),
    }


def _cache_path(kind: str, case_id: str) -> Path:
    return ROOT / "cached_hidden" / kind / f"{case_id}.pt"


def prepare() -> dict[str, Any]:
    config = NativeMicroConfig()
    config.validate()
    ctx = context()
    ROOT.mkdir(parents=True, exist_ok=True)
    for name in ("dataset", "cached_hidden", "checkpoints", "evaluations", "final"):
        (ROOT / name).mkdir(exist_ok=True)
    fixture_manifest = json.loads((REPO / ".ralf_run/recursive_domain_qwen3_solver/fixture_manifest.json").read_text())
    if fixture_manifest["fixture_set_hash"] != FIXTURE_HASH:
        raise RuntimeError("qwen3_micro_fixture_hash_mismatch")
    if sha256_file(GOLD_PACKETS) != "b8372471c530a50896c6082bb245550635d72cdb6eb349988e337eb70b0a725c":
        raise RuntimeError("qwen3_micro_gold_packet_hash_mismatch")
    if json.loads(REAL_PACKET_MANIFEST.read_text())["real_packet_set_hash"] != "92aceb8ad5749a45a528286ecb44ccb33f7804a1e36db538d39620615253934a":
        raise RuntimeError("qwen3_micro_real_packet_hash_mismatch")
    protocol = json.loads(PROTOCOL_SUMMARY.read_text())
    if protocol["best_variant"] != "B_newline_prompt_strict" or not protocol["gate"]["passed"]:
        raise RuntimeError("qwen3_micro_direct_baseline_not_authorized")
    dataset = {
        "case_order": list(CANARY_CASE_IDS),
        "cases": [ctx["cases"][item] for item in CANARY_CASE_IDS],
        "traces": [ctx["traces"][item] for item in CANARY_CASE_IDS],
        "canonical_targets": ctx["targets"],
        "no_chain_of_thought": True,
    }
    dump(ROOT / "dataset" / "micro_dataset.json", dataset)
    dataset_hash = sha256_file(ROOT / "dataset" / "micro_dataset.json")

    records: dict[str, Any] = {}
    planner, planner_tokenizer, planner_load = _load_model(QWEN25_3B)
    try:
        for case_id in CANARY_CASE_IDS:
            case, trace = ctx["cases"][case_id], ctx["traces"][case_id]
            prompt = build_domain_planner_prompt(domain_payload(case))
            real = ctx["upstream"]["cases"][case_id]["planner"]["structured_output"] or {}
            for kind, assistant in (("planner_gold", trace["planner_target"]), ("planner_real", real)):
                text = json.dumps(assistant, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                hidden, mask, details = extract_assistant_hidden(planner, planner_tokenizer, prompt=prompt, assistant=text)
                records[f"{kind}:{case_id}"] = save_hidden_cache(
                    _cache_path(kind, case_id),
                    tensor=hidden,
                    attention_mask=mask,
                    metadata={
                        "case_id": case_id,
                        "cache_kind": kind,
                        "model_id": MODEL_SPECS["planner"]["model_id"],
                        "model_revision": MODEL_SPECS["planner"]["revision"],
                        **details,
                    },
                )
    finally:
        _unload(planner, planner_tokenizer)

    critic, critic_tokenizer, critic_load = _load_model(QWEN25_15B)
    try:
        for case_id in CANARY_CASE_IDS:
            case, trace = ctx["cases"][case_id], ctx["traces"][case_id]
            real_stage = ctx["upstream"]["cases"][case_id]
            pairs = (
                ("critic_gold", trace["planner_target"], trace["critic_target"]),
                (
                    "critic_real",
                    real_stage["planner"]["structured_output"] or {},
                    real_stage["critic"]["structured_output"] or {},
                ),
            )
            for kind, planner_value, critic_value in pairs:
                planner_text = json.dumps(planner_value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                prompt = build_domain_critic_prompt_with_slot(domain_payload(case)).replace(PLANNER_SLOT, planner_text)
                assistant = json.dumps(critic_value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                hidden, mask, details = extract_assistant_hidden(critic, critic_tokenizer, prompt=prompt, assistant=assistant)
                records[f"{kind}:{case_id}"] = save_hidden_cache(
                    _cache_path(kind, case_id),
                    tensor=hidden,
                    attention_mask=mask,
                    metadata={
                        "case_id": case_id,
                        "cache_kind": kind,
                        "model_id": MODEL_SPECS["critic"]["model_id"],
                        "model_revision": MODEL_SPECS["critic"]["revision"],
                        **details,
                    },
                )
    finally:
        _unload(critic, critic_tokenizer)

    adapters = initialize_native_adapters(config.seed)
    adapter_details = adapter_manifest(adapters)
    initial_hashes = {}
    for name, module in adapters.items():
        record = save_adapter_checkpoint(
            module,
            ROOT / "checkpoints" / "initial" / f"{name}.pt",
            metadata={
                "namespace": "recursive_domain_qwen3_micro_overfit_fresh",
                "adapter": name,
                "step": 0,
                "seed": config.seed,
                "dataset_hash": dataset_hash,
                "fixture_hash": FIXTURE_HASH,
            },
        )
        initial_hashes[name] = record["sha256"]
    qwen3_tokenizer = AutoTokenizer.from_pretrained(QWEN3, local_files_only=True, trust_remote_code=True, use_fast=True)
    target_lengths = select_target_length(qwen3_tokenizer, ctx["targets"], config)
    del qwen3_tokenizer
    tokenizer_hashes = {
        "planner": tokenizer_sha256(QWEN25_3B),
        "critic": tokenizer_sha256(QWEN25_15B),
        "solver": stable_sha256(
            {
                name: sha256_file(QWEN3 / name)
                for name in ("merges.txt", "tokenizer.json", "tokenizer_config.json", "vocab.json")
            }
        ),
    }
    manifest = micro_manifest(
        config=config,
        tokenizer_hashes=tokenizer_hashes,
        adapter_shapes=adapter_details,
        dataset_hash=dataset_hash,
        evidence_packet_hash=sha256_file(GOLD_PACKETS),
    )
    manifest.update(
        {
            "initial_checkpoint_hashes": initial_hashes,
            "target_sequence_probe": target_lengths,
            "cache_records": records,
            "base_loads": {"planner": planner_load, "critic": critic_load},
            "parser": "strict",
            "canonical_prompt_unchanged": True,
        }
    )
    dump(ROOT / "manifest.json", manifest)
    graph = native_graph()
    for edge in graph:
        adapter_name = edge.get("adapter")
        if adapter_name in set(adapters):
            runtime_dtype = "torch.float32; cast to frozen base dtype at inputs_embeds boundary"
        elif adapter_name == "Qwen3_frozen_solver":
            runtime_dtype = "torch.float16"
        else:
            runtime_dtype = None
        edge.update(
            {
                "runtime_dtype": runtime_dtype,
                "runtime_device": "cuda:0 stagewise" if edge.get("used_by_final_decode") else "not_loaded",
                "attention_mask": "ones for pooled latent; chat mask preserved",
                "requires_grad": adapter_name in set(adapters),
            }
        )
    dump(
        ROOT / "native_graph.json",
        {
            "profile_id": "recursive_mas_domain_qwen3_solver_v1",
            "rounds": 1,
            "edges": graph,
            "cache_pooling": "one masked-mean assistant hidden per case",
            "outer31": "excluded_next_round_only",
            "final_decode": "Qwen3 frozen logits -> strict parser -> domain_opinion_v1",
            "hooks": records,
        },
    )
    return manifest


def load_initial_adapters(device: str = "cpu") -> nn.ModuleDict:
    manifest = json.loads((ROOT / "manifest.json").read_text())
    adapters = initialize_native_adapters(manifest["training_config"]["seed"])
    for name, module in adapters.items():
        load_adapter_checkpoint(
            module,
            ROOT / "checkpoints" / "initial" / f"{name}.pt",
            expected_sha256=manifest["initial_checkpoint_hashes"][name],
        )
    return adapters.to(device)


def load_cached(kind: str, case_id: str) -> torch.Tensor:
    model_role = "planner" if kind.startswith("planner") else "critic"
    value, mask, _ = load_hidden_cache(
        _cache_path(kind, case_id),
        expected={
            "case_id": case_id,
            "cache_kind": kind,
            "model_revision": MODEL_SPECS[model_role]["revision"],
        },
    )
    if mask.tolist() != [[1]]:
        raise RuntimeError("qwen3_micro_cache_mask_invalid")
    return value


def adapter_latent(adapters: nn.ModuleDict, critic_hidden: torch.Tensor, *, device: str) -> torch.Tensor:
    dtype = next(adapters["critic_inner"].parameters()).dtype
    value = critic_hidden.to(device=device, dtype=dtype)
    return adapters["solver_inner"](adapters["outer23"](adapters["critic_inner"](value)))


def generate_solver(
    model: Any,
    tokenizer: Any,
    *,
    prompt: str,
    latent: torch.Tensor | None,
) -> dict[str, Any]:
    prompt_ids = torch.tensor(chat_ids(tokenizer, prompt), dtype=torch.long, device="cuda:0").unsqueeze(0)
    started = time.monotonic()
    if latent is None:
        output = model.generate(
            input_ids=prompt_ids,
            attention_mask=torch.ones_like(prompt_ids),
            do_sample=False,
            temperature=None,
            top_p=None,
            top_k=None,
            max_new_tokens=512,
            use_cache=True,
            eos_token_id=model.generation_config.eos_token_id,
            pad_token_id=tokenizer.pad_token_id,
        )
        token_ids = output[0, prompt_ids.shape[1] :]
    else:
        with torch.no_grad():
            prompt_embed = model.get_input_embeddings()(prompt_ids)
        full = torch.cat((prompt_embed, latent.to(dtype=prompt_embed.dtype)), dim=1)
        output = model.generate(
            inputs_embeds=full,
            attention_mask=torch.ones(full.shape[:2], dtype=torch.long, device=full.device),
            do_sample=False,
            temperature=None,
            top_p=None,
            top_k=None,
            max_new_tokens=512,
            use_cache=True,
            eos_token_id=model.generation_config.eos_token_id,
            pad_token_id=tokenizer.pad_token_id,
        )
        token_ids = output[0]
    torch.cuda.synchronize()
    raw = tokenizer.decode(token_ids.detach().cpu().tolist(), skip_special_tokens=True).strip()
    return {
        "raw": raw,
        "tokens": int(token_ids.numel()),
        "wall_ms": (time.monotonic() - started) * 1000.0,
        "raw_sha256": hashlib.sha256(raw.encode()).hexdigest(),
    }


def score_outputs(
    name: str,
    outputs: Mapping[str, Mapping[str, Any]],
    *,
    packet_kind: str,
    ctx: Mapping[str, Any],
) -> dict[str, Any]:
    packets = ctx[f"{packet_kind}_packets"]
    results = []
    for case_id in CANARY_CASE_IDS:
        case, trace, packet = ctx["cases"][case_id], ctx["traces"][case_id], packets[case_id]
        scored = evaluate_solver_final(
            final_text=outputs[case_id]["raw"],
            case=case,
            trace=trace,
            packet=packet,
        )
        rule = reference_accuracy(packet.planner_rule_ids, case["gold"]["required_rules"], packet.allowed_rule_ids)
        source = reference_accuracy(packet.planner_source_ids, case["gold"]["required_sources"], packet.allowed_source_ids)
        packet_complete = rule == 1.0 and source == 1.0
        safety = any(str(item).casefold() in outputs[case_id]["raw"].casefold() for item in case["gold"].get("forbidden_claims", []))
        pipeline_error = classify_pipeline_error(
            packet_complete=packet_complete,
            solver_valid=bool(scored["semantic_complete"]),
            serialization_valid=bool(scored["final_schema_valid"]),
            solver_error_class=scored["error_class"],
        )
        row = {
            "case_id": case_id,
            **{key: value for key, value in outputs[case_id].items() if key != "raw"},
            **scored,
            "rule_accuracy": rule,
            "source_accuracy": source,
            "packet_complete": packet_complete,
            "pipeline_error": pipeline_error,
            "safety_violation": safety,
        }
        case_dir = ROOT / "evaluations" / name / case_id
        case_dir.mkdir(parents=True, exist_ok=True)
        (case_dir / "raw_sanitized.txt").write_text(outputs[case_id]["raw"] + "\n", encoding="utf-8")
        dump(case_dir / "result.json", {key: value for key, value in row.items() if key not in {"payload", "metadata"}})
        results.append(row)
    base = aggregate_solver_results(results)
    valid_packet = [item for item in results if item["packet_complete"]]
    mean_valid = lambda field: sum(bool(item[field]) for item in valid_packet) / len(valid_packet)
    metrics = {
        **base,
        "name": name,
        "valid_count": base["semantic_complete_count"],
        "schema_valid_count": base["final_schema_valid_count"],
        "rule_accuracy": sum(float(item["rule_accuracy"]) for item in results) / 8,
        "source_accuracy": sum(float(item["source_accuracy"]) for item in results) / 8,
        "valid_packet_contradiction_inclusion": mean_valid("contradiction_inclusion"),
        "valid_packet_counterargument_coverage": mean_valid("counterargument_coverage"),
        "invented_ids": base["foreign_ids_accepted"],
        "demo_contamination": base["demo_contamination_count"],
        "safety_violations": sum(bool(item["safety_violation"]) for item in results),
        "approval_violations": 0,
        "upstream_errors": sum(item["pipeline_error"] == "upstream_provenance_error" for item in results),
        "solver_errors": sum(item["pipeline_error"] in {"solver_semantic_error", "solver_format_error"} for item in results),
        "mean_wall_ms": sum(float(item.get("wall_ms") or 0) for item in results) / 8,
    }
    dump(ROOT / "evaluations" / name / "summary.json", metrics)
    return metrics


def native_critic_hidden_fresh(adapters: nn.ModuleDict, ctx: Mapping[str, Any], *, label: str) -> dict[str, torch.Tensor]:
    planner_latents = {}
    adapters = adapters.to("cuda:0", dtype=torch.float32).eval()
    with torch.inference_mode():
        for case_id in CANARY_CASE_IDS:
            value = load_cached("planner_real", case_id).to("cuda:0", dtype=torch.float32)
            planner_latents[case_id] = adapters["outer12"](adapters["planner_inner"](value)).cpu()
    adapters.to("cpu")
    torch.cuda.empty_cache()
    critic, tokenizer, _ = _load_model(QWEN25_15B)
    output = {}
    records = {}
    try:
        embed = critic.get_input_embeddings()
        for case_id in CANARY_CASE_IDS:
            template = build_domain_critic_prompt_with_slot(domain_payload(ctx["cases"][case_id]))
            prefix, suffix = template.split(PLANNER_SLOT, 1)
            prefix_ids = torch.tensor(tokenizer(prefix, add_special_tokens=False)["input_ids"], device="cuda:0").unsqueeze(0)
            suffix_ids = torch.tensor(tokenizer(suffix, add_special_tokens=False)["input_ids"], device="cuda:0").unsqueeze(0)
            with torch.inference_mode():
                full = torch.cat(
                    (
                        embed(prefix_ids),
                        planner_latents[case_id].to("cuda:0", dtype=embed.weight.dtype),
                        embed(suffix_ids),
                    ),
                    dim=1,
                )
                result = critic(
                    inputs_embeds=full,
                    attention_mask=torch.ones(full.shape[:2], dtype=torch.long, device="cuda:0"),
                    output_hidden_states=True,
                    use_cache=False,
                    return_dict=True,
                )
                hidden = result.hidden_states[-1][:, -1:, :].float().cpu()
            output[case_id] = hidden
            records[case_id] = save_hidden_cache(
                _cache_path(label, case_id),
                tensor=hidden,
                attention_mask=torch.ones((1, 1), dtype=torch.long),
                metadata={
                    "case_id": case_id,
                    "cache_kind": label,
                    "model_id": MODEL_SPECS["critic"]["model_id"],
                    "model_revision": MODEL_SPECS["critic"]["revision"],
                    "input_hash": stable_sha256({"template": template, "planner_latent": planner_latents[case_id].float().tolist()}),
                    "source_sequence_length": int(full.shape[1]),
                    "pooled_sequence_length": 1,
                    "pooling": "last_native_critic_hidden",
                },
            )
    finally:
        _unload(critic, tokenizer)
    dump(ROOT / "cached_hidden" / label / "manifest.json", records)
    return output


def baseline() -> dict[str, Any]:
    ctx = context()
    adapters = load_initial_adapters()
    direct = {
        case_id: {
            "raw": ctx["targets"][case_id],
            "tokens": json.loads((DIRECT_TARGETS / case_id / "classification.json").read_text()).get("tokens", 0),
            "wall_ms": 0.0,
            "source": "verified_protocol_canary_raw",
        }
        for case_id in CANARY_CASE_IDS
    }
    direct_metrics = score_outputs("zero_A_direct", direct, packet_kind="real", ctx=ctx)
    if direct_metrics["valid_count"] != 8 or direct_metrics["schema_valid_count"] != 8:
        dump(ROOT / "evaluations" / "gate_zero.json", {"classification": "baseline_regression", "direct": direct_metrics})
        raise RuntimeError("baseline_regression")
    native_fresh = native_critic_hidden_fresh(adapters, ctx, label="critic_native_fresh")
    solver, tokenizer, load = _load_model(QWEN3)
    torch.cuda.reset_peak_memory_stats()
    adapters = adapters.to("cuda:0", dtype=torch.float16).eval()
    variants = {"zero_B_gold_fresh": {}, "zero_C_real_fresh": {}, "zero_D_native_fresh": {}}
    try:
        with torch.inference_mode():
            for case_id in CANARY_CASE_IDS:
                case = ctx["cases"][case_id]
                gold_critic = ctx["traces"][case_id]["critic_target"]
                real_critic = ctx["upstream"]["cases"][case_id]["critic"]["structured_output"] or {}
                for name, kind, packet, critic_value in (
                    ("zero_B_gold_fresh", "critic_gold", ctx["gold_packets"][case_id], gold_critic),
                    ("zero_C_real_fresh", "critic_real", ctx["real_packets"][case_id], real_critic),
                ):
                    latent = adapter_latent(adapters, load_cached(kind, case_id), device="cuda:0")
                    variants[name][case_id] = generate_solver(
                        solver,
                        tokenizer,
                        prompt=solver_prompt(case, critic_value, packet),
                        latent=latent,
                    )
                latent = adapter_latent(adapters, native_fresh[case_id], device="cuda:0")
                variants["zero_D_native_fresh"][case_id] = generate_solver(
                    solver,
                    tokenizer,
                    prompt=solver_prompt(case, real_critic, ctx["real_packets"][case_id]),
                    latent=latent,
                )
        metrics = {
            "A": direct_metrics,
            "B": score_outputs("zero_B_gold_fresh", variants["zero_B_gold_fresh"], packet_kind="gold", ctx=ctx),
            "C": score_outputs("zero_C_real_fresh", variants["zero_C_real_fresh"], packet_kind="real", ctx=ctx),
            "D": score_outputs("zero_D_native_fresh", variants["zero_D_native_fresh"], packet_kind="real", ctx=ctx),
            "resources": {
                **load,
                "gpu_peak_bytes": int(torch.cuda.max_memory_allocated()),
                "ram_peak_bytes": int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024),
            },
            "training_executed": False,
        }
    finally:
        adapters.to("cpu")
        _unload(solver, tokenizer)
    dump(ROOT / "evaluations" / "gate_zero.json", metrics)
    return metrics


def _training_tensors(
    model: Any,
    tokenizer: Any,
    *,
    prompt: str,
    target: str,
    latent: torch.Tensor,
) -> tuple[torch.Tensor, torch.Tensor, dict[str, int]]:
    prompt_ids = torch.tensor(chat_ids(tokenizer, prompt), device="cuda:0", dtype=torch.long).unsqueeze(0)
    target_list = tokenizer(target, add_special_tokens=False)["input_ids"] + [tokenizer.eos_token_id]
    target_ids = torch.tensor(target_list, device="cuda:0", dtype=torch.long).unsqueeze(0)
    target_mask = torch.ones_like(target_ids)
    embed = model.get_input_embeddings()
    with torch.no_grad():
        prompt_embed = embed(prompt_ids)
        response_embed = embed(target_ids)
    full = torch.cat((prompt_embed, latent.to(dtype=prompt_embed.dtype), response_embed), dim=1)
    prompt_length = int(prompt_embed.shape[1] + latent.shape[1])
    labels = response_only_loss_labels(prompt_length, target_ids, target_mask)
    attention = torch.ones(full.shape[:2], dtype=torch.long, device="cuda:0")
    return full, labels, {
        "prompt_tokens": prompt_length,
        "response_tokens": int(target_ids.shape[1]),
        "masked_input_tokens": int((labels[:, :prompt_length] == -100).sum()),
        "supervised_response_tokens": int((labels[:, prompt_length:] != -100).sum()),
        "attention_tokens": int(attention.sum()),
    }


def evaluate_with_adapters(
    model: Any,
    tokenizer: Any,
    adapters: nn.ModuleDict,
    ctx: Mapping[str, Any],
    *,
    cache_kind: str,
    packet_kind: str,
    name: str,
    hidden_override: Mapping[str, torch.Tensor] | None = None,
) -> dict[str, Any]:
    outputs = {}
    adapters.eval()
    model.eval()
    with torch.inference_mode():
        for case_id in CANARY_CASE_IDS:
            hidden = hidden_override[case_id] if hidden_override is not None else load_cached(cache_kind, case_id)
            latent = adapter_latent(adapters, hidden, device="cuda:0")
            critic = (
                ctx["traces"][case_id]["critic_target"]
                if packet_kind == "gold"
                else ctx["upstream"]["cases"][case_id]["critic"]["structured_output"] or {}
            )
            outputs[case_id] = generate_solver(
                model,
                tokenizer,
                prompt=solver_prompt(ctx["cases"][case_id], critic, ctx[f"{packet_kind}_packets"][case_id]),
                latent=latent,
            )
    return score_outputs(name, outputs, packet_kind=packet_kind, ctx=ctx)


def train_stage_a() -> tuple[nn.ModuleDict, dict[str, Any]]:
    config = NativeMicroConfig()
    ctx = context()
    manifest = json.loads((ROOT / "manifest.json").read_text())
    target_probe = manifest["target_sequence_probe"]
    adapters = load_initial_adapters().to("cuda:0", dtype=torch.float32)
    for name, module in adapters.items():
        active = name in {"critic_inner", "outer23", "solver_inner"}
        for parameter in module.parameters():
            parameter.requires_grad = active
    trainable = [parameter for module in (adapters["critic_inner"], adapters["outer23"], adapters["solver_inner"]) for parameter in module.parameters()]
    optimizer = torch.optim.AdamW(trainable, lr=config.learning_rate, betas=(0.9, 0.95))
    model, tokenizer, load = _load_model(QWEN3, train_solver=True)
    torch.cuda.reset_peak_memory_stats()
    losses = []
    steps = []
    best_gate = None
    stale = 0
    try:
        for step in range(1, config.max_steps + 1):
            started = time.monotonic()
            optimizer.zero_grad(set_to_none=True)
            accumulated_ce = 0.0
            accumulated_total = 0.0
            mask_audit = None
            for offset in range(config.gradient_accumulation):
                case_id = CANARY_CASE_IDS[((step - 1) * config.gradient_accumulation + offset) % 8]
                case = ctx["cases"][case_id]
                hidden = load_cached("critic_gold", case_id).to("cuda:0", dtype=torch.float32)
                critic_latent = adapters["critic_inner"](hidden)
                solver_hidden = adapters["outer23"](critic_latent)
                solver_latent = adapters["solver_inner"](solver_hidden)
                full, labels, mask_audit = _training_tensors(
                    model,
                    tokenizer,
                    prompt=solver_prompt(case, ctx["traces"][case_id]["critic_target"], ctx["gold_packets"][case_id]),
                    target=ctx["targets"][case_id],
                    latent=solver_latent,
                )
                with torch.autocast(device_type="cuda", dtype=torch.float16):
                    result = model.model(
                        inputs_embeds=full,
                        attention_mask=torch.ones(full.shape[:2], dtype=torch.long, device="cuda:0"),
                        use_cache=False,
                        return_dict=True,
                    )
                    shift_labels = labels[:, 1:].contiguous()
                    response_positions = shift_labels != -100
                    response_hidden = result.last_hidden_state[:, :-1, :][response_positions]
                    response_targets = shift_labels[response_positions]
                    shift_logits = model.lm_head(response_hidden).float()
                    ce = functional.cross_entropy(
                        shift_logits,
                        response_targets,
                    )
                    regularization = 0.5 * (critic_latent.float().square().mean() + solver_latent.float().square().mean())
                    total = config.final_token_ce_weight * ce + config.latent_regularization_weight * regularization
                (total / config.gradient_accumulation).backward()
                accumulated_ce += float(ce.detach().cpu())
                accumulated_total += float(total.detach().cpu())
                del result, shift_logits, shift_labels, response_positions, response_hidden, response_targets, full, labels
            gradients = adapter_gradient_report(adapters, detached_upstream=True)
            if not all(math.isfinite(value) for value in gradients["norms"].values()):
                raise RuntimeError("non_finite_adapter_gradient")
            torch.nn.utils.clip_grad_norm_(trainable, config.gradient_clip)
            optimizer.step()
            step_row = {
                "step": step,
                "final_token_ce": accumulated_ce / config.gradient_accumulation,
                "total_loss": accumulated_total / config.gradient_accumulation,
                "gradient_norms": gradients["norms"],
                "mask": mask_audit,
                "target_sequence_selected": target_probe["selected"],
                "allocated_gpu_bytes": int(torch.cuda.memory_allocated()),
                "reserved_gpu_bytes": int(torch.cuda.memory_reserved()),
                "ram_process_bytes": int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024),
                "step_ms": (time.monotonic() - started) * 1000.0,
            }
            steps.append(step_row)
            losses.append(step_row["final_token_ce"])
            if step % config.checkpoint_every == 0:
                checkpoint_records = {}
                for name in ("critic_inner", "outer23", "solver_inner"):
                    checkpoint_records[name] = save_adapter_checkpoint(
                        adapters[name],
                        ROOT / "checkpoints" / f"stage_a_step_{step:03d}" / f"{name}.pt",
                        metadata={
                            "stage": "A",
                            "step": step,
                            "dataset_hash": manifest["dataset_hash"],
                            "fixture_hash": FIXTURE_HASH,
                        },
                    )
                dump(ROOT / "checkpoints" / f"stage_a_step_{step:03d}" / "manifest.json", checkpoint_records)
            if step % config.evaluate_every == 0:
                model.eval()
                metrics = evaluate_with_adapters(
                    model,
                    tokenizer,
                    adapters,
                    ctx,
                    cache_kind="critic_gold",
                    packet_kind="gold",
                    name=f"stage_a_step_{step:03d}",
                )
                model.train()
                gate = stage_a_gate(metrics)
                dump(ROOT / "evaluations" / f"stage_a_step_{step:03d}" / "gate.json", gate)
                if gate["passed"]:
                    best_gate = {"step": step, "metrics": metrics, "gate": gate}
                    break
                stale += 1
                if stale >= config.early_stopping_patience:
                    break
        summary = {
            "stage": "A",
            "steps": len(steps),
            "loss_initial": losses[0] if losses else None,
            "loss_final": losses[-1] if losses else None,
            "steps_log": steps,
            "best_gate": best_gate,
            "gradient_report": steps[-1]["gradient_norms"] if steps else {},
            "base_model": {**load, "frozen": True},
            "gpu_peak_bytes": int(torch.cuda.max_memory_allocated()),
            "ram_peak_bytes": int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024),
            "oom": False,
        }
        dump(ROOT / "evaluations" / "stage_a_summary.json", summary)
        if best_gate is None:
            summary["classification"] = "critic_solver_bridge_not_trainable"
            return adapters, summary
        return adapters, summary
    except torch.cuda.OutOfMemoryError as exc:
        optimizer.zero_grad(set_to_none=True)
        torch.cuda.empty_cache()
        summary = {
            "stage": "A",
            "classification": "local_vram_insufficient",
            "error": type(exc).__name__,
            "steps": len(steps),
            "loss_initial": losses[0] if losses else None,
            "loss_final": losses[-1] if losses else None,
            "target_sequence_selected": target_probe["selected"],
            "full_prompt_preserved": True,
            "oom": True,
        }
        dump(ROOT / "evaluations" / "stage_a_summary.json", summary)
        return adapters, summary
    finally:
        model.gradient_checkpointing_disable()
        model.config.use_cache = True
        _unload(model, tokenizer)
        adapters.to("cpu")


def train_upstream_stagewise(adapters: nn.ModuleDict, ctx: Mapping[str, Any]) -> dict[str, Any]:
    config = NativeMicroConfig()
    adapters = adapters.to("cuda:0", dtype=torch.float32)
    for name, module in adapters.items():
        active = name in {"planner_inner", "outer12"}
        for parameter in module.parameters():
            parameter.requires_grad = active
    trainable = [parameter for name in ("planner_inner", "outer12") for parameter in adapters[name].parameters()]
    optimizer = torch.optim.AdamW(trainable, lr=config.learning_rate, betas=(0.9, 0.95))
    rows = []
    for step in range(1, 11):
        optimizer.zero_grad(set_to_none=True)
        planner_total = 0.0
        critic_total = 0.0
        for offset in range(config.gradient_accumulation):
            case_id = CANARY_CASE_IDS[((step - 1) * config.gradient_accumulation + offset) % 8]
            planner_real = load_cached("planner_real", case_id).to("cuda:0", dtype=torch.float32)
            planner_gold = load_cached("planner_gold", case_id).to("cuda:0", dtype=torch.float32)
            critic_gold = load_cached("critic_gold", case_id).to("cuda:0", dtype=torch.float32)
            planner_prediction = adapters["planner_inner"](planner_real)
            critic_prediction = adapters["outer12"](planner_prediction)
            planner_loss = functional.mse_loss(planner_prediction, planner_gold)
            critic_loss = functional.mse_loss(critic_prediction, critic_gold)
            latent = 0.5 * (planner_prediction.square().mean() + critic_prediction.square().mean())
            total = (
                config.planner_structured_weight * planner_loss
                + config.critic_structured_weight * critic_loss
                + config.latent_regularization_weight * latent
            )
            (total / config.gradient_accumulation).backward()
            planner_total += float(planner_loss.detach().cpu())
            critic_total += float(critic_loss.detach().cpu())
        torch.nn.utils.clip_grad_norm_(trainable, config.gradient_clip)
        optimizer.step()
        rows.append(
            {
                "step": step,
                "planner_structured_loss": planner_total / config.gradient_accumulation,
                "critic_structured_loss": critic_total / config.gradient_accumulation,
                "planner_inner_gradient_norm": float(sum((parameter.grad.float().square().sum() for parameter in adapters["planner_inner"].parameters() if parameter.grad is not None), torch.tensor(0.0, device="cuda:0")).sqrt()),
                "outer12_gradient_norm": float(sum((parameter.grad.float().square().sum() for parameter in adapters["outer12"].parameters() if parameter.grad is not None), torch.tensor(0.0, device="cuda:0")).sqrt()),
                "final_ce_reaches_upstream": False,
                "detach_classification": "structured_surrogate_only",
            }
        )
    adapters.to("cpu")
    summary = {
        "stage": "C_upstream_surrogate",
        "steps": rows,
        "final_ce_reaches_planner_inner": False,
        "final_ce_reaches_outer12": False,
        "training_claim": "structured_surrogate_only",
    }
    dump(ROOT / "evaluations" / "stage_c_upstream_training.json", summary)
    return summary


def save_final(adapters: nn.ModuleDict, metrics: Mapping[str, Any], *, step: int) -> dict[str, Any]:
    manifest = json.loads((ROOT / "manifest.json").read_text())
    records = {}
    for name, module in adapters.items():
        records[name] = save_adapter_checkpoint(
            module,
            ROOT / "final" / f"{name}.pt",
            metadata={
                "stage": "C",
                "step": step,
                "dataset_hash": manifest["dataset_hash"],
                "fixture_hash": FIXTURE_HASH,
                "model_revisions": {role: value["revision"] for role, value in MODEL_SPECS.items()},
                "gate_metrics": dict(metrics),
            },
        )
    manifest["checkpoint_hashes"] = {name: value["sha256"] for name, value in records.items()}
    dump(ROOT / "manifest.json", manifest)
    dump(ROOT / "final" / "manifest.json", records)
    return records


def train_all() -> dict[str, Any]:
    baseline_metrics = json.loads((ROOT / "evaluations" / "gate_zero.json").read_text())
    if baseline_metrics["A"]["valid_count"] != 8:
        raise RuntimeError("baseline_regression")
    adapters, stage_a = train_stage_a()
    if not stage_a.get("best_gate"):
        output = {"stage_a": stage_a, "classification": stage_a.get("classification", "critic_solver_bridge_not_trainable")}
        dump(ROOT / "evaluations" / "micro_overfit_summary.json", output)
        return output
    ctx = context()
    model, tokenizer, load = _load_model(QWEN3)
    adapters = adapters.to("cuda:0", dtype=torch.float32)
    try:
        stage_b_metrics = evaluate_with_adapters(
            model,
            tokenizer,
            adapters,
            ctx,
            cache_kind="critic_real",
            packet_kind="real",
            name="stage_b_real_critic",
        )
        stage_b_result = stage_b_gate(stage_b_metrics)
    finally:
        adapters.to("cpu")
        _unload(model, tokenizer)
    dump(ROOT / "evaluations" / "stage_b_gate.json", stage_b_result)
    if not stage_b_result["passed"]:
        output = {
            "stage_a": stage_a,
            "stage_b": {"metrics": stage_b_metrics, "gate": stage_b_result},
            "classification": stage_b_result["classification"],
        }
        dump(ROOT / "evaluations" / "micro_overfit_summary.json", output)
        return output
    upstream = train_upstream_stagewise(adapters, ctx)
    native_hidden = native_critic_hidden_fresh(adapters, ctx, label="critic_native_trained")
    model, tokenizer, load_c = _load_model(QWEN3)
    adapters = adapters.to("cuda:0", dtype=torch.float32)
    try:
        stage_c_metrics = evaluate_with_adapters(
            model,
            tokenizer,
            adapters,
            ctx,
            cache_kind="critic_native_trained",
            packet_kind="real",
            name="stage_c_native_complete",
            hidden_override=native_hidden,
        )
        stage_c_result = stage_c_gate(stage_c_metrics)
    finally:
        adapters.to("cpu")
        _unload(model, tokenizer)
    dump(ROOT / "evaluations" / "stage_c_gate.json", stage_c_result)
    checkpoints = {}
    if stage_c_result["passed"]:
        checkpoints = save_final(adapters, stage_c_metrics, step=int(stage_a["best_gate"]["step"]))
    output = {
        "baseline": baseline_metrics,
        "stage_a": stage_a,
        "stage_b": {"metrics": stage_b_metrics, "gate": stage_b_result},
        "stage_c_upstream": upstream,
        "stage_c": {"metrics": stage_c_metrics, "gate": stage_c_result},
        "classification": stage_c_result["classification"],
        "checkpoints": checkpoints,
        "test_split_executed": False,
        "training_complete_dataset_executed": False,
    }
    dump(ROOT / "evaluations" / "micro_overfit_summary.json", output)
    return output


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("phase", choices=("prepare", "baseline", "train", "all"))
    args = parser.parse_args()
    torch.manual_seed(42)
    if args.phase == "prepare":
        result = prepare()
    elif args.phase == "baseline":
        result = baseline()
    elif args.phase == "train":
        result = train_all()
    else:
        prepare()
        baseline()
        result = train_all()
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True, default=str))


if __name__ == "__main__":
    main()
