from __future__ import annotations

import argparse
import gc
import hashlib
import json
from pathlib import Path
import resource
import threading
import time
from typing import Any, Mapping

import requests
import torch
from torch import nn
from transformers import AutoModelForCausalLM, AutoTokenizer, StoppingCriteria, StoppingCriteriaList

from ralfloop_agent.domains.domain_opinion import build_domain_reasoning_prompt, parse_domain_opinion
from ralfloop_agent.domains.recursive_mas_domain_dataset import CATEGORIES, deterministic_splits, generate_dataset
from ralfloop_agent.domains.recursive_mas_domain_prompts import PLANNER_SLOT, build_domain_critic_prompt_with_slot, build_domain_planner_prompt
from ralfloop_agent.domains.recursive_mas_domain_provenance import build_provenance_solver_prompt
from ralfloop_agent.domains.recursive_mas_qwen3_heldout_benchmark import (
    HELDOUT_SEED,
    PRIOR_CANARY_IDS,
    RUNNERS,
    aggregate,
    audit_checkpoints,
    blind_outputs,
    differing_case_ids,
    input_payload,
    limited_gate,
    parse_single_domain_output,
    request_for_case,
    score_output,
    select_heldout_cases,
    sha256_file,
    stable_sha256,
    validate_runner_timing,
)
from ralfloop_agent.domains.recursive_mas_qwen3_micro_overfit import initialize_native_adapters, load_adapter_checkpoint
from ralfloop_agent.domains.recursive_mas_qwen3_solver_eval import build_real_packet, evaluate_solver_final
from ralfloop_agent.providers.llama_cpp_server import LlamaCppServerManager


REPO = Path("/home/sibilla-cumana/ralfloop_agent_scaffold")
ROOT = REPO / ".ralf_run/recursive_domain_qwen3_heldout_benchmark"
QWEN25_3B = Path("/home/sibilla-cumana/RecursiveMAS/.hf-domain-instruct-v1/hub/models--Qwen--Qwen2.5-3B-Instruct/snapshots/aa8e72537993ba99e69dfaafa59ed015b17504d1")
QWEN25_15B = Path("/home/sibilla-cumana/RecursiveMAS/.hf-domain-instruct-v1/hub/models--Qwen--Qwen2.5-1.5B-Instruct/snapshots/989aa7980e4cf806f80c7fef2b1adb7bc71aa306")
QWEN3 = Path("/home/sibilla-cumana/RecursiveMAS/.hf-domain-qwen3-solver-v1/hub/models--Qwen--Qwen3-1.7B/snapshots/70d244cc86ccca08cf5af4e1e306ecf908b1ad5e")
PROFILE = REPO / "ralfloop_agent/domains/recursive_mas_domain_qwen3_solver_v1.json"
FINAL = REPO / ".ralf_run/recursive_domain_qwen3_micro_overfit/final"
INITIAL = REPO / ".ralf_run/recursive_domain_qwen3_micro_overfit/checkpoints/initial"
MICRO_MANIFEST = REPO / ".ralf_run/recursive_domain_qwen3_micro_overfit/manifest.json"
STRICT_SUFFIX = (
    "\nSTRICT PROVENANCE OUTPUT: Never copy any literal identifier beginning with R_ or S_. "
    "Describe evidence meaning only; provenance remains exclusively in the upstream packet."
)


def dump(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _rss_bytes() -> int:
    return int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024)


def _load_model(snapshot: Path) -> tuple[Any, Any, float]:
    started = time.monotonic()
    tokenizer = AutoTokenizer.from_pretrained(snapshot, local_files_only=True, trust_remote_code=True, use_fast=True)
    model = AutoModelForCausalLM.from_pretrained(
        snapshot, local_files_only=True, trust_remote_code=True, dtype=torch.float16, low_cpu_mem_usage=True
    )
    for parameter in model.parameters():
        parameter.requires_grad = False
    model.eval().to("cuda:0")
    return model, tokenizer, (time.monotonic() - started) * 1000.0


def _unload(model: Any, tokenizer: Any) -> None:
    model.to("cpu")
    del model, tokenizer
    gc.collect()
    torch.cuda.empty_cache()
    torch.cuda.synchronize()


def _chat_ids(tokenizer: Any, prompt: str, assistant: str | None = None) -> list[int]:
    messages = [{"role": "user", "content": prompt}]
    if assistant is not None:
        messages.append({"role": "assistant", "content": assistant})
    ids = tokenizer.apply_chat_template(
        messages, tokenize=True, add_generation_prompt=assistant is None, enable_thinking=False
    )
    if isinstance(ids, Mapping):
        ids = ids["input_ids"]
    if hasattr(ids, "tolist"):
        ids = ids.tolist()
    if ids and isinstance(ids[0], list):
        ids = ids[0]
    return [int(item) for item in ids]


class _FirstToken(StoppingCriteria):
    def __init__(self, started: float) -> None:
        self.started = started
        self.ttft_ms: float | None = None

    def __call__(self, input_ids: torch.LongTensor, scores: torch.FloatTensor, **kwargs: Any) -> bool:
        del input_ids, scores, kwargs
        if self.ttft_ms is None:
            torch.cuda.synchronize()
            self.ttft_ms = (time.monotonic() - self.started) * 1000.0
        return False


def generate_timed(
    model: Any,
    tokenizer: Any,
    *,
    prompt: str,
    latent: torch.Tensor | None = None,
    max_new_tokens: int = 512,
) -> dict[str, Any]:
    prompt_ids = torch.tensor(_chat_ids(tokenizer, prompt), device="cuda:0", dtype=torch.long).unsqueeze(0)
    started = time.monotonic()
    timer = _FirstToken(started)
    kwargs = {
        "do_sample": False,
        "temperature": None,
        "top_p": None,
        "top_k": None,
        "max_new_tokens": max_new_tokens,
        "use_cache": True,
        "eos_token_id": model.generation_config.eos_token_id,
        "pad_token_id": tokenizer.pad_token_id,
        "stopping_criteria": StoppingCriteriaList([timer]),
    }
    if latent is None:
        output = model.generate(input_ids=prompt_ids, attention_mask=torch.ones_like(prompt_ids), **kwargs)
        generated = output[0, prompt_ids.shape[1] :]
    else:
        with torch.inference_mode():
            prompt_embed = model.get_input_embeddings()(prompt_ids)
        full = torch.cat((prompt_embed, latent.to(device="cuda:0", dtype=prompt_embed.dtype)), dim=1)
        output = model.generate(
            inputs_embeds=full,
            attention_mask=torch.ones(full.shape[:2], dtype=torch.long, device="cuda:0"),
            **kwargs,
        )
        generated = output[0]
    torch.cuda.synchronize()
    wall_ms = (time.monotonic() - started) * 1000.0
    raw = tokenizer.decode(generated.detach().cpu().tolist(), skip_special_tokens=True).strip()
    eos_ids = model.generation_config.eos_token_id
    eos_set = {int(eos_ids)} if isinstance(eos_ids, int) else {int(item) for item in eos_ids or []}
    return {
        "raw": raw,
        "token_ids": generated.detach().cpu().tolist(),
        "tokens": int(generated.numel()),
        "ttft_ms": min(float(timer.ttft_ms or wall_ms), wall_ms),
        "generation_wall_ms": wall_ms,
        "eos": bool(eos_set & set(generated.detach().cpu().tolist())),
        "timeout": False,
    }


def planner_prompt(case: Mapping[str, Any]) -> str:
    return build_domain_planner_prompt(json.dumps(input_payload(case), ensure_ascii=False, sort_keys=True, separators=(",", ":"))) + (
        "\nOUTPUT JSON ONLY, no markdown. Exact keys: questions, facts_selected, rules_selected, sources_selected, "
        "interpretations, missing_information. Select only supplied fact/rule/source IDs. interpretations is a list "
        "of objects with id, summary, refs. No action or approval."
    )


def critic_prompt(case: Mapping[str, Any], planner: Mapping[str, Any]) -> str:
    native = build_domain_critic_prompt_with_slot(
        json.dumps(input_payload(case), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    ).replace(PLANNER_SLOT, json.dumps(planner, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    return native + (
        "\nOUTPUT JSON ONLY, no markdown. Exact keys: criticisms, contradictions, rule_application_errors, "
        "source_provenance_errors, strongest_counterargument, unresolved_issues. Use only supplied IDs. "
        "No action or approval."
    )


def solver_prompt(case: Mapping[str, Any], critic: Mapping[str, Any], packet: Any) -> str:
    return build_provenance_solver_prompt(
        case, critic, packet, request_slots=False, demo_mode="none", strict_newlines=True
    ) + STRICT_SUFFIX


def _json_object(raw: str) -> dict[str, Any] | None:
    value = parse_domain_opinion(raw)
    return dict(value) if isinstance(value, Mapping) else None


def _filter_planner(case: Mapping[str, Any], value: Mapping[str, Any] | None) -> dict[str, Any]:
    raw = dict(value or {})
    allowed_facts = {str(item["fact_id"]) for item in case["facts"]}
    allowed_rules = {str(item["rule_id"]) for item in case["rules"]}
    allowed_sources = {str(item["source_id"]) for item in case["sources"]}
    for key, allowed in (
        ("facts_selected", allowed_facts),
        ("rules_selected", allowed_rules),
        ("sources_selected", allowed_sources),
    ):
        raw[key] = [str(item) for item in raw.get(key, []) if str(item) in allowed]
    for key in ("questions", "interpretations", "missing_information"):
        if not isinstance(raw.get(key), list):
            raw[key] = []
    return raw


def _native_planner_hidden(model: Any, tokenizer: Any, case: Mapping[str, Any], planner: Mapping[str, Any]) -> dict[str, Any]:
    prompt = build_domain_planner_prompt(json.dumps(input_payload(case), ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    assistant = json.dumps(planner, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    prompt_ids = _chat_ids(tokenizer, prompt)
    full_ids = _chat_ids(tokenizer, prompt, assistant)
    ids = torch.tensor(full_ids, device="cuda:0", dtype=torch.long).unsqueeze(0)
    with torch.inference_mode():
        result = model(input_ids=ids, attention_mask=torch.ones_like(ids), output_hidden_states=True, use_cache=False, return_dict=True)
    response_end = len(full_ids) - 1
    response_start = min(len(prompt_ids), response_end - 1)
    hidden = result.hidden_states[-1][:, response_start:response_end, :].float().mean(dim=1, keepdim=True).cpu()
    return {
        "tensor": hidden.tolist(),
        "shape": list(hidden.shape),
        "dtype": str(hidden.dtype),
        "attention_mask": [1],
        "sequence_length": int(response_end - response_start),
        "tensor_sha256": hashlib.sha256(hidden.contiguous().numpy().tobytes()).hexdigest(),
    }


def run_upstream(cases: list[Mapping[str, Any]]) -> dict[str, Any]:
    output: dict[str, Any] = {"cases": {}}
    planner_model, planner_tokenizer, planner_load = _load_model(QWEN25_3B)
    torch.cuda.reset_peak_memory_stats()
    try:
        for case in cases:
            generated = generate_timed(planner_model, planner_tokenizer, prompt=planner_prompt(case))
            structured = _filter_planner(case, _json_object(generated["raw"]))
            hidden = _native_planner_hidden(planner_model, planner_tokenizer, case, structured)
            output["cases"][case["id"]] = {"planner": {**generated, "structured_output": structured}, "planner_hidden": hidden}
    finally:
        output["planner_load_ms"] = planner_load
        output["planner_gpu_peak_bytes"] = int(torch.cuda.max_memory_allocated())
        _unload(planner_model, planner_tokenizer)
    critic_model, critic_tokenizer, critic_load = _load_model(QWEN25_15B)
    torch.cuda.reset_peak_memory_stats()
    try:
        for case in cases:
            row = output["cases"][case["id"]]
            generated = generate_timed(critic_model, critic_tokenizer, prompt=critic_prompt(case, row["planner"]["structured_output"]))
            structured = _json_object(generated["raw"]) or {}
            packet = build_real_packet(case, row["planner"]["structured_output"], structured)
            row["critic"] = {**generated, "structured_output": structured}
            row["evidence_packet"] = packet.to_dict()
            row["evidence_packet_hash"] = stable_sha256(packet.to_dict())
    finally:
        output["critic_load_ms"] = critic_load
        output["critic_gpu_peak_bytes"] = int(torch.cuda.max_memory_allocated())
        _unload(critic_model, critic_tokenizer)
    output["ram_peak_bytes"] = _rss_bytes()
    dump(ROOT / "upstream.json", output)
    return output


def _load_adapters(kind: str) -> nn.ModuleDict:
    adapters = initialize_native_adapters(42)
    if kind == "fresh":
        manifest = json.loads(MICRO_MANIFEST.read_text())
        hashes = manifest["initial_checkpoint_hashes"]
        root = INITIAL
    elif kind == "trained":
        final_manifest = json.loads((FINAL / "manifest.json").read_text())
        hashes = {name: record["sha256"] for name, record in final_manifest.items()}
        root = FINAL
    else:
        raise ValueError("adapter_kind_invalid")
    for name, module in adapters.items():
        load_adapter_checkpoint(module, root / f"{name}.pt", expected_sha256=hashes[name])
    return adapters


def _native_critic_hidden(
    cases: list[Mapping[str, Any]], upstream: Mapping[str, Any], adapters: nn.ModuleDict, *, omit: str | None = None
) -> tuple[dict[str, torch.Tensor], dict[str, Any]]:
    adapters = adapters.to("cuda:0", dtype=torch.float32).eval()
    planner_latents = {}
    with torch.inference_mode():
        for case in cases:
            hidden = upstream["cases"][case["id"]]["planner_hidden"]["tensor"]
            if not isinstance(hidden, torch.Tensor):
                hidden = torch.tensor(hidden)
            value = hidden.to("cuda:0", dtype=torch.float32)
            if omit == "planner_inner":
                latent = adapters["outer12"](value)
            elif omit == "outer12":
                latent = torch.zeros((value.shape[0], value.shape[1], 1536), device="cuda:0", dtype=torch.float32)
            else:
                latent = adapters["outer12"](adapters["planner_inner"](value))
            planner_latents[case["id"]] = latent.cpu()
    adapters.to("cpu")
    torch.cuda.empty_cache()
    critic, tokenizer, load_ms = _load_model(QWEN25_15B)
    torch.cuda.reset_peak_memory_stats()
    output, audit = {}, {}
    try:
        embed = critic.get_input_embeddings()
        for case in cases:
            template = build_domain_critic_prompt_with_slot(json.dumps(input_payload(case), ensure_ascii=False, sort_keys=True, separators=(",", ":")))
            prefix, suffix = template.split(PLANNER_SLOT, 1)
            prefix_ids = torch.tensor(tokenizer(prefix, add_special_tokens=False)["input_ids"], device="cuda:0").unsqueeze(0)
            suffix_ids = torch.tensor(tokenizer(suffix, add_special_tokens=False)["input_ids"], device="cuda:0").unsqueeze(0)
            started = time.monotonic()
            with torch.inference_mode():
                full = torch.cat((embed(prefix_ids), planner_latents[case["id"]].to("cuda:0", dtype=embed.weight.dtype), embed(suffix_ids)), dim=1)
                result = critic(inputs_embeds=full, attention_mask=torch.ones(full.shape[:2], dtype=torch.long, device="cuda:0"), output_hidden_states=True, use_cache=False, return_dict=True)
                hidden = result.hidden_states[-1][:, -1:, :].float().cpu()
            output[case["id"]] = hidden
            audit[case["id"]] = {
                "shape": list(hidden.shape), "dtype": str(hidden.dtype), "attention_mask": [1],
                "sequence_length": int(full.shape[1]), "input_hash": stable_sha256(input_payload(case)),
                "tensor_sha256": hashlib.sha256(hidden.contiguous().numpy().tobytes()).hexdigest(),
                "wall_ms": (time.monotonic() - started) * 1000.0, "omitted_adapter": omit,
            }
    finally:
        resources = {"load_ms": load_ms, "gpu_peak_bytes": int(torch.cuda.max_memory_allocated()), "ram_peak_bytes": _rss_bytes()}
        _unload(critic, tokenizer)
    return output, {"cases": audit, **resources}


def _packet(case: Mapping[str, Any], upstream: Mapping[str, Any]) -> Any:
    row = upstream["cases"][case["id"]]
    return build_real_packet(case, row["planner"]["structured_output"], row["critic"]["structured_output"])


def _score_solver_row(
    case: Mapping[str, Any], upstream: Mapping[str, Any], generated: Mapping[str, Any], runner_id: str,
    *, load_ms: float, native_wall_ms: float = 0.0, gpu_peak_bytes: int = 0,
) -> dict[str, Any]:
    packet = _packet(case, upstream)
    evaluated = evaluate_solver_final(final_text=generated["raw"], case=case, trace={"case_id": case["id"]}, packet=packet)
    payload = evaluated.get("payload")
    planner = upstream["cases"][case["id"]]["planner"]["structured_output"]
    score = score_output(
        case, generated["raw"], payload, parser_error=evaluated.get("parse_error"),
        planner_rule_ids=planner.get("rules_selected") or [], planner_source_ids=planner.get("sources_selected") or [], runner_id=runner_id,
    )
    upstream_row = upstream["cases"][case["id"]]
    upstream_wall = float(upstream_row["planner"]["generation_wall_ms"]) + float(upstream_row["critic"]["generation_wall_ms"])
    generation = float(generated["generation_wall_ms"])
    row = {
        "case_id": case["id"], "runner_id": runner_id, "input_hash": stable_sha256(input_payload(case)),
        "raw_bounded_output": generated["raw"], "parser_result": {key: value for key, value in evaluated.items() if key not in {"payload", "metadata"}},
        "domain_opinion_v1": payload, "validation_result": score["validation_errors"],
        "timings": {
            "load_ms": float(load_ms), "ttft_ms": float(generated["ttft_ms"]),
            "generation_wall_ms": generation,
            "wall_total_ms": float(load_ms + upstream_wall + native_wall_ms + generation),
        },
        "tokens_produced": int(generated["tokens"]), "gpu_peak_bytes": int(gpu_peak_bytes),
        "ram_peak_bytes": _rss_bytes(), "timeout": bool(generated["timeout"]), **score,
    }
    validate_runner_timing(row)
    return row


def run_qwen3_runner(
    runner_id: str, cases: list[Mapping[str, Any]], upstream: Mapping[str, Any],
    *, adapter_kind: str | None, native_hidden: Mapping[str, torch.Tensor] | None = None,
    native_audit: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    model, tokenizer, load_total = _load_model(QWEN3)
    adapters = _load_adapters(adapter_kind).to("cuda:0", dtype=torch.float32).eval() if adapter_kind else None
    torch.cuda.reset_peak_memory_stats()
    rows = []
    per_case_load = load_total / len(cases)
    try:
        with torch.inference_mode():
            for case in cases:
                packet = _packet(case, upstream)
                latent = None
                if adapters is not None and native_hidden is not None:
                    hidden = native_hidden[case["id"]].to("cuda:0", dtype=torch.float32)
                    latent = adapters["solver_inner"](adapters["outer23"](adapters["critic_inner"](hidden)))
                generated = generate_timed(model, tokenizer, prompt=solver_prompt(case, upstream["cases"][case["id"]]["critic"]["structured_output"], packet), latent=latent)
                rows.append(_score_solver_row(
                    case, upstream, generated, runner_id, load_ms=per_case_load,
                    native_wall_ms=float((native_audit or {}).get("cases", {}).get(case["id"], {}).get("wall_ms", 0)),
                    gpu_peak_bytes=int(torch.cuda.max_memory_allocated()),
                ))
                dump(ROOT / "runs" / runner_id / f"{case['id']}.json", rows[-1])
    finally:
        if adapters is not None:
            adapters.to("cpu")
        _unload(model, tokenizer)
    return rows


def _llama_stream(prompt: str) -> dict[str, Any]:
    started = time.monotonic()
    first = None
    content = []
    tokens = 0
    with requests.post(
        "http://127.0.0.1:19091/v1/chat/completions",
        json={
            "model": "qwen2.5:7b", "messages": [{"role": "system", "content": "Rispondi in italiano. Segui esattamente il contratto JSON."}, {"role": "user", "content": prompt}],
            "temperature": 0, "seed": 42, "max_tokens": 512, "stream": True, "cache_prompt": True,
        }, timeout=(3, 180), stream=True,
    ) as response:
        response.raise_for_status()
        for line in response.iter_lines(decode_unicode=True):
            if not line or not line.startswith("data: "):
                continue
            data = line[6:]
            if data == "[DONE]":
                break
            body = json.loads(data)
            delta = body.get("choices", [{}])[0].get("delta", {}).get("content") or ""
            if delta:
                if first is None:
                    first = (time.monotonic() - started) * 1000.0
                content.append(delta)
            usage = body.get("usage") or {}
            tokens = max(tokens, int(usage.get("completion_tokens") or 0))
    wall = (time.monotonic() - started) * 1000.0
    return {"raw": "".join(content).strip(), "tokens": tokens, "ttft_ms": min(float(first or wall), wall), "generation_wall_ms": wall, "timeout": False}


def run_single_domain(cases: list[Mapping[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    manager = LlamaCppServerManager()
    load_started = time.monotonic()
    state = manager.ensure_available()
    load_ms = (time.monotonic() - load_started) * 1000.0
    rows = []
    try:
        for case in cases:
            generated = _llama_stream(build_domain_reasoning_prompt(request_for_case(case)))
            payload, parser_error = parse_single_domain_output(generated["raw"], case)
            score = score_output(
                case,
                generated["raw"],
                payload,
                parser_error=parser_error,
                planner_rule_ids=case["gold"]["required_rules"],
                planner_source_ids=case["gold"]["required_sources"],
                runner_id="A_single_domain",
            )
            row = {
                "case_id": case["id"], "runner_id": "A_single_domain", "input_hash": stable_sha256(input_payload(case)),
                "raw_bounded_output": generated["raw"], "parser_result": {"error": parser_error},
                "domain_opinion_v1": payload, "validation_result": score["validation_errors"],
                "timings": {"load_ms": load_ms / len(cases), "ttft_ms": generated["ttft_ms"], "generation_wall_ms": generated["generation_wall_ms"], "wall_total_ms": load_ms / len(cases) + generated["generation_wall_ms"]},
                "tokens_produced": generated["tokens"], "gpu_peak_bytes": 0, "ram_peak_bytes": _rss_bytes(), "timeout": False, **score,
            }
            validate_runner_timing(row)
            rows.append(row)
            dump(ROOT / "runs/A_single_domain" / f"{case['id']}.json", row)
    finally:
        stopped = manager.stop()
    return rows, {"manager_start": state, "manager_stop": stopped, "temporary_lab_process": True, "service_restart": False}


def _ablation_latent(
    adapters: nn.ModuleDict, hidden: torch.Tensor, omitted: str
) -> torch.Tensor:
    value = hidden.to("cuda:0", dtype=torch.float32)
    if omitted == "critic_inner":
        return adapters["solver_inner"](adapters["outer23"](value))
    critic_value = adapters["critic_inner"](value)
    if omitted == "outer23":
        return adapters["solver_inner"](
            torch.zeros((value.shape[0], value.shape[1], 2048), device="cuda:0", dtype=torch.float32)
        )
    solver_value = adapters["outer23"](critic_value)
    if omitted == "solver_inner":
        return solver_value
    return adapters["solver_inner"](solver_value)


def run_ablation_phase() -> dict[str, Any]:
    summary = json.loads((ROOT / "summary.json").read_text())
    selected_data = json.loads((ROOT / "selected_cases.json").read_text())
    case_by_id = {case["id"]: case for case in selected_data["cases"]}
    difference_ids = list(summary["fresh_trained_different_case_ids"])
    if not difference_ids:
        summary["ablation"] = {"executed": False, "reason": "no_metric_level_differences"}
        dump(ROOT / "summary.json", summary)
        return summary["ablation"]
    cases = [case_by_id[case_id] for case_id in difference_ids]
    upstream = json.loads((ROOT / "upstream.json").read_text())
    trained = _load_adapters("trained")
    standard_hidden, standard_audit = _native_critic_hidden(cases, upstream, trained)
    d1_hidden, d1_audit = _native_critic_hidden(cases, upstream, _load_adapters("trained"), omit="planner_inner")
    d2_hidden, d2_audit = _native_critic_hidden(cases, upstream, _load_adapters("trained"), omit="outer12")
    native = {
        "D1_without_planner_inner": (d1_hidden, d1_audit, "planner_inner"),
        "D2_without_outer12": (d2_hidden, d2_audit, "outer12"),
        "D3_without_critic_inner": (standard_hidden, standard_audit, "critic_inner"),
        "D4_without_outer23": (standard_hidden, standard_audit, "outer23"),
        "D5_without_solver_inner": (standard_hidden, standard_audit, "solver_inner"),
    }
    model, tokenizer, load_total = _load_model(QWEN3)
    adapters = _load_adapters("trained").to("cuda:0", dtype=torch.float32).eval()
    output: dict[str, Any] = {}
    try:
        for variant, (hidden_values, audit, omitted) in native.items():
            rows = []
            torch.cuda.reset_peak_memory_stats()
            with torch.inference_mode():
                for case in cases:
                    packet = _packet(case, upstream)
                    if omitted in {"planner_inner", "outer12"}:
                        hidden = hidden_values[case["id"]].to("cuda:0", dtype=torch.float32)
                        latent = adapters["solver_inner"](adapters["outer23"](adapters["critic_inner"](hidden)))
                    else:
                        latent = _ablation_latent(adapters, hidden_values[case["id"]], omitted)
                    generated = generate_timed(
                        model,
                        tokenizer,
                        prompt=solver_prompt(case, upstream["cases"][case["id"]]["critic"]["structured_output"], packet),
                        latent=latent,
                    )
                    row = _score_solver_row(
                        case,
                        upstream,
                        generated,
                        f"D_ablation_{variant}",
                        load_ms=load_total / (len(cases) * len(native)),
                        native_wall_ms=float(audit["cases"][case["id"]]["wall_ms"]),
                        gpu_peak_bytes=int(torch.cuda.max_memory_allocated()),
                    )
                    rows.append(row)
                    dump(ROOT / "ablation" / variant / f"{case['id']}.json", row)
            output[variant] = {"omitted_adapter": omitted, "case_count": len(cases), "aggregate": aggregate(rows)}
    finally:
        adapters.to("cpu")
        _unload(model, tokenizer)
    trained_rows = {
        path.stem: json.loads(path.read_text())
        for path in (ROOT / "runs/D_recursive_trained").glob("domain_adapter_*.json")
        if path.stem in difference_ids
    }
    for variant, record in output.items():
        rows = {
            path.stem: json.loads(path.read_text())
            for path in (ROOT / "ablation" / variant).glob("domain_adapter_*.json")
        }
        record["worsened_cases"] = sorted(
            case_id
            for case_id in difference_ids
            if int(rows[case_id]["semantic_complete"]) + int(rows[case_id]["schema_valid"])
            < int(trained_rows[case_id]["semantic_complete"]) + int(trained_rows[case_id]["schema_valid"])
        )
        record["improved_cases"] = sorted(
            case_id
            for case_id in difference_ids
            if int(rows[case_id]["semantic_complete"]) + int(rows[case_id]["schema_valid"])
            > int(trained_rows[case_id]["semantic_complete"]) + int(trained_rows[case_id]["schema_valid"])
        )
    ablation = {"executed": True, "case_ids": difference_ids, "variants": output}
    summary["ablation"] = ablation
    dump(ROOT / "ablation/summary.json", ablation)
    dump(ROOT / "summary.json", summary)
    return ablation


def prepare() -> dict[str, Any]:
    ROOT.mkdir(parents=True, exist_ok=True)
    cases, traces = generate_dataset()
    splits = deterministic_splits(cases)
    selected, contamination = select_heldout_cases(cases, splits, excluded_ids=PRIOR_CANARY_IDS)
    trace_by_id = {row["case_id"]: row for row in traces}
    profile = json.loads(PROFILE.read_text())
    final_manifest = json.loads((FINAL / "manifest.json").read_text())
    checkpoint_audit = audit_checkpoints(profile, final_manifest, REPO)
    math_profile = REPO / "ralfloop_agent/domains/recursive_mas_profiles.py"
    telegram_gate = REPO / "ralfloop_agent/domains/domain_approval_executor.py"
    manifest = {
        "protocol": "recursive_domain_qwen3_heldout_limited_v1", "seed": HELDOUT_SEED,
        "selection": [{"case_id": case["id"], "category": case["category"], "input_hash": stable_sha256(input_payload(case)), "gold_hash": stable_sha256(trace_by_id[case["id"]])} for case in selected],
        "contamination_audit": contamination, "excluded_prior_canary_ids": list(PRIOR_CANARY_IDS),
        "split_sizes": {name: len(value) for name, value in splits.items()}, "test_only": True,
        "training_or_validation_overlap": False, "semantic_prompt_examples": [], "checkpoint_audit": checkpoint_audit,
        "profile_enabled": False, "runtime_registered": False, "outer31_present": False,
        "fixture_hash": stable_sha256([input_payload(case) for case in selected]),
        "math_profile_sha256": sha256_file(math_profile), "telegram_gate_sha256": sha256_file(telegram_gate),
        "training_executed": False, "models_downloaded": False, "full_36_benchmark_executed": False,
    }
    dump(ROOT / "manifest.json", manifest)
    dump(ROOT / "selected_cases.json", {"cases": selected, "traces": [trace_by_id[case["id"]] for case in selected]})
    return manifest


def run() -> dict[str, Any]:
    selected_data = json.loads((ROOT / "selected_cases.json").read_text())
    cases = selected_data["cases"]
    results: dict[str, list[dict[str, Any]]] = {}
    results["A_single_domain"], single_runtime = run_single_domain(cases)
    upstream = run_upstream(cases)
    results["B_qwen3_direct"] = run_qwen3_runner("B_qwen3_direct", cases, upstream, adapter_kind=None)
    fresh_adapters = _load_adapters("fresh")
    fresh_hidden, fresh_audit = _native_critic_hidden(cases, upstream, fresh_adapters)
    dump(ROOT / "runs/C_recursive_fresh/native_hidden_audit.json", fresh_audit)
    results["C_recursive_fresh"] = run_qwen3_runner("C_recursive_fresh", cases, upstream, adapter_kind="fresh", native_hidden=fresh_hidden, native_audit=fresh_audit)
    trained_adapters = _load_adapters("trained")
    trained_hidden, trained_audit = _native_critic_hidden(cases, upstream, trained_adapters)
    dump(ROOT / "runs/D_recursive_trained/native_hidden_audit.json", trained_audit)
    results["D_recursive_trained"] = run_qwen3_runner("D_recursive_trained", cases, upstream, adapter_kind="trained", native_hidden=trained_hidden, native_audit=trained_audit)
    aggregates = {name: aggregate(rows) for name, rows in results.items()}
    gate = limited_gate(aggregates)
    blind, mapping = blind_outputs(results)
    dump(ROOT / "blind_review.json", {"rubric": ["argument_strength", "counterargument_quality", "contradiction_handling", "uncertainty_clarity", "recommendation_usefulness", "balance"], "automatic_review": False, "candidates": blind})
    key_path = ROOT / "blind_mapping.json"
    dump(key_path, mapping)
    key_path.chmod(0o600)
    differences = differing_case_ids(results["C_recursive_fresh"], results["D_recursive_trained"])
    summary = {
        "aggregates": aggregates, "gate": gate, "single_runtime": single_runtime,
        "fresh_trained_different_case_ids": differences,
        "ablation": {"executed": False, "reason": "no_metric_level_differences" if not differences else "pending"},
        "full_36_benchmark_executed": False, "training_executed": False, "human_review_executed": False,
        "model_judge_used": False,
    }
    dump(ROOT / "summary.json", summary)
    return summary


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("phase", choices=("prepare", "run", "ablation", "all"))
    args = parser.parse_args()
    torch.manual_seed(HELDOUT_SEED)
    if args.phase == "prepare":
        result = prepare()
    elif args.phase == "run":
        result = run()
    elif args.phase == "ablation":
        result = run_ablation_phase()
    else:
        prepare()
        run()
        result = run_ablation_phase()
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
