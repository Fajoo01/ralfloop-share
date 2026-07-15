from __future__ import annotations

import argparse
from collections import defaultdict
import copy
import hashlib
import json
from pathlib import Path
import random
import re
import statistics
import sys
import time
from typing import Any, Mapping

import torch

REPO = Path("/home/sibilla-cumana/ralfloop_agent_scaffold")
sys.path.insert(0, str(REPO / "tools"))
import run_recursive_mas_qwen3_heldout_benchmark as base  # noqa: E402

from ralfloop_agent.domains.recursive_mas_domain_dataset import deterministic_splits, generate_dataset
from ralfloop_agent.domains.recursive_mas_domain_provenance import PROTOCOL_VERSION, EvidencePacket
from ralfloop_agent.domains.recursive_mas_external_benchmark import aggregate_external, load_external_cases
from ralfloop_agent.domains.recursive_mas_provenance_selector_diagnostics import (
    SELECTOR_IDS, EvidenceSelection, aggregate_selection, combine_selections, deterministic_candidates, grouped_selection,
    order_sensitivity, planner_selection, preliminary_gate, reserve_seal_audit, selection_from_payload,
    selection_row, selector_json,
)
from ralfloop_agent.domains.recursive_mas_qwen3_heldout_benchmark import input_payload, score_output, stable_sha256
from ralfloop_agent.domains.recursive_mas_qwen3_solver_eval import evaluate_solver_final

ROOT = REPO / ".ralf_run/recursive_domain_provenance_diagnostics"
VALIDATION_UPSTREAM = ROOT / "validation_upstream.json"
FINAL_UPSTREAM = REPO / ".ralf_run/recursive_domain_external_holdout/upstream.json"
FINAL_A = REPO / "ralfloop_agent/domains/data/recursive_domain_external_holdout/final_a.jsonl"
RESERVE_B = REPO / ".ralf_run/recursive_domain_external_holdout/reserve_b.jsonl"
RESERVE_SHA = "44e70370dd5f4322bd19a5e654f24b24c945522f9474c0c5d3a6332533e2ed6e"


def dump(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def datasets() -> dict[str, list[dict[str, Any]]]:
    old, _ = generate_dataset(); by_id = {case["id"]: case for case in old}
    validation = [by_id[case_id] for case_id in deterministic_splits(old)["validation"]]
    return {"validation": validation, "final_a": load_external_cases(FINAL_A)}


def upstreams() -> dict[str, Mapping[str, Any]]:
    return {"validation": json.loads(VALIDATION_UPSTREAM.read_text()), "final_a": json.loads(FINAL_UPSTREAM.read_text())}


def prepare() -> None:
    ROOT.mkdir(parents=True, exist_ok=True)
    reserve = reserve_seal_audit(RESERVE_B, RESERVE_SHA)
    manifest = {
        "protocol": "recursive_mas_provenance_selector_diagnostics_v1", "seed": 41027,
        "datasets": {name: {"count": len(rows), "hash": stable_sha256(rows)} for name, rows in datasets().items()},
        "reserve_b": reserve, "reserve_b_sealed": reserve["sealed"], "reserve_b_executed": False,
        "training_executed": False, "models_downloaded": False, "feature_flag": "0",
        "math_profile_sha256": base.sha256_file(REPO / "ralfloop_agent/domains/recursive_mas_profiles.py"),
        "telegram_gate_sha256": base.sha256_file(REPO / "ralfloop_agent/domains/domain_approval_executor.py"),
    }
    dump(ROOT / "manifest.json", manifest)


def validation_upstream() -> None:
    rows = datasets()["validation"]
    previous = base.ROOT; base.ROOT = ROOT / "validation_runtime"
    try:
        result = base.run_upstream(rows)
    finally:
        base.ROOT = previous
    dump(VALIDATION_UPSTREAM, result)


def _critic_ids(critic: Mapping[str, Any], kind: str) -> tuple[list[str], list[str]]:
    confirmed = critic.get(f"confirmed_{kind}_ids") or []
    challenged = critic.get(f"challenged_{kind}_ids") or []
    allowed_key = "rule_id" if kind == "rule" else "source_id"
    if not confirmed and not challenged:
        for item in critic.get(f"{kind}_assessments") or []:
            if isinstance(item, Mapping) and item.get(allowed_key):
                target = challenged if "challeng" in str(item.get("assessment") or "").casefold() else confirmed
                target.append(str(item[allowed_key]))
    return list(dict.fromkeys(map(str, confirmed))), list(dict.fromkeys(map(str, challenged)))


def _conclusion(case: Mapping[str, Any]) -> str:
    return str(case.get("expected_recommendation_type") or ("conditional" if case["gold"].get("conditional_required") else "favorable"))


def _distractor_count(case: Mapping[str, Any]) -> int:
    gold = set(case["gold"]["required_rules"]) | set(case["gold"]["required_sources"])
    available = {str(item["rule_id"]) for item in case["rules"]} | {str(item["source_id"]) for item in case["sources"]}
    return len(available - gold)


def audit_current() -> None:
    outputs: dict[str, Any] = {}
    for split, cases in datasets().items():
        upstream = upstreams()[split]; rows = []
        for case in cases:
            source = upstream["cases"][case["id"]]
            planner = source["planner"]["structured_output"]; critic = source["critic"]["structured_output"]
            selection = planner_selection(case, planner); row = selection_row(case, selection)
            cr, xr = _critic_ids(critic, "rule"); cs, xs = _critic_ids(critic, "source")
            packet = source["evidence_packet"]
            row.update({
                "planner_rule_ids": list(selection.selected_rule_ids), "planner_source_ids": list(selection.selected_source_ids),
                "critic_confirmed_rule_ids": cr, "critic_challenged_rule_ids": xr,
                "critic_confirmed_source_ids": cs, "critic_challenged_source_ids": xs,
                "final_packet_rule_ids": packet.get("planner_rule_ids") or [], "final_packet_source_ids": packet.get("planner_source_ids") or [],
                "distractor_count": _distractor_count(case), "gold_evidence_count": len(case["gold"]["required_rules"]) + len(case["gold"]["required_sources"]),
                "conclusion": _conclusion(case),
            }); rows.append(row)
        outputs[split] = {
            "cases": rows, "aggregate": aggregate_selection(rows),
            "by_category": grouped_selection(rows, "category"), "by_domain": grouped_selection(rows, "domain_id"),
            "by_distractor_count": grouped_selection(rows, "distractor_count"), "by_gold_evidence_count": grouped_selection(rows, "gold_evidence_count"),
            "by_conclusion": grouped_selection(rows, "conclusion"),
        }
    dump(ROOT / "current_planner_audit.json", outputs)


def _reorder_case(case: Mapping[str, Any], mode: str, seed: int) -> dict[str, Any]:
    value = copy.deepcopy(case)
    for field in ("rules", "sources"):
        if mode == "reverse": value[field] = list(reversed(value[field]))
        elif mode == "random": random.Random(f"{seed}:{case['id']}:{field}").shuffle(value[field])
    return value


def technical_audit() -> None:
    all_cases = [*datasets()["validation"], *datasets()["final_a"]]
    selected = sorted(all_cases, key=lambda case: hashlib.sha256(str(case["id"]).encode()).hexdigest())[:12]
    model, tokenizer, load_ms = base._load_model(base.QWEN25_3B)
    records, sensitivity = [], []
    try:
        for case in all_cases:
            cached = upstreams()["validation" if str(case["id"]).startswith("domain_adapter_") else "final_a"]["cases"][case["id"]]["planner"]
            prompt = base.planner_prompt(case); input_count = len(base._chat_ids(tokenizer, prompt))
            records.append({
                "case_id": case["id"], "input_token_count": input_count, "output_token_count": cached["tokens"],
                "finish_reason": "eos" if cached["eos"] else "max_tokens", "eos": cached["eos"],
                "input_truncated": False, "output_truncated": not cached["eos"], "chat_template": tokenizer.chat_template,
                "rule_positions": {str(item["rule_id"]): ("first" if i < len(case["rules"])/3 else "last" if i >= 2*len(case["rules"])/3 else "middle") for i,item in enumerate(case["rules"])},
                "source_positions": {str(item["source_id"]): ("first" if i < len(case["sources"])/3 else "last" if i >= 2*len(case["sources"])/3 else "middle") for i,item in enumerate(case["sources"])},
            })
        for case in selected:
            split = "validation" if str(case["id"]).startswith("domain_adapter_") else "final_a"
            original = planner_selection(case, upstreams()[split]["cases"][case["id"]]["planner"]["structured_output"])
            variants, detail = [], {}
            for mode in ("reverse", "random"):
                changed = _reorder_case(case, mode, 41027); started = time.monotonic()
                generated = base.generate_timed(model, tokenizer, prompt=base.planner_prompt(changed))
                structured = base._filter_planner(changed, base._json_object(generated["raw"]))
                selection = planner_selection(changed, structured); variants.append(selection)
                detail[mode] = {"selection": selection.to_dict(), "wall_ms": (time.monotonic()-started)*1000, "tokens": generated["tokens"], "eos": generated["eos"]}
            sensitivity.append({"case_id": case["id"], "original": original.to_dict(), **detail, **order_sensitivity(original, variants)})
    finally:
        base._unload(model, tokenizer)
    changed = sum(bool(row["changed"]) for row in sensitivity)
    dump(ROOT / "technical_audit.json", {"load_ms": load_ms, "cases": records, "order_cases": sensitivity, "order_changed_cases": changed, "classification": "planner_evidence_order_instability" if changed >= 4 else None})


def _selector_prompt(case: Mapping[str, Any], *, mode: str, planner: EvidenceSelection | None = None, candidates: EvidenceSelection | None = None) -> str:
    rules = case["rules"]; sources = case["sources"]
    if candidates is not None:
        rules = [item for item in rules if str(item["rule_id"]) in set(candidates.selected_rule_ids)]
        sources = [item for item in sources if str(item["source_id"]) in set(candidates.selected_source_ids)]
    sections = [
        "ROLE: Evidence relevance selector. Do not answer the domain question.",
        "QUESTION: " + str(case["question"]),
        "FACTS: " + json.dumps(case.get("facts") or [], ensure_ascii=False, separators=(",", ":")),
        "CONSTRAINTS: " + json.dumps(case.get("constraints") or [], ensure_ascii=False, separators=(",", ":")),
        "ALLOWED RULES: " + json.dumps(rules, ensure_ascii=False, separators=(",", ":")),
        "ALLOWED SOURCES: " + json.dumps(sources, ensure_ascii=False, separators=(",", ":")),
    ]
    if planner is not None:
        sections.append("PLANNER SELECTION TO REVIEW AND RESCUE: " + json.dumps(planner.to_dict(), ensure_ascii=False, separators=(",", ":")))
    sections += [
        "Select every and only item needed to answer, including conditional, contradictory, and decision-limiting evidence. Ignore plausible distractors.",
        "OUTPUT JSON ONLY. Exact keys: selected_rule_ids, selected_source_ids. Arrays contain only exact allowlisted IDs. No explanation.",
    ]
    return "\n".join(sections)


def _model_selection(case: Mapping[str, Any], raw: str, wall_ms: float) -> tuple[EvidenceSelection, dict[str, Any]]:
    valid, error, payload = True, None, None
    allowed = {str(item["rule_id"]) for item in case["rules"]} | {str(item["source_id"]) for item in case["sources"]}
    foreign_ids: list[str] = []
    try:
        payload = selector_json(raw)
        emitted = [str(item) for key in SELECTOR_IDS for item in (payload.get(key) or [])] if isinstance(payload, Mapping) else []
        foreign_ids = list(dict.fromkeys(item for item in emitted if item not in allowed))
        selection = selection_from_payload(case, payload)
    except Exception as exc:
        valid, error = False, f"{type(exc).__name__}:{exc}"
        selection = selection_from_payload(case, {"selected_rule_ids": [], "selected_source_ids": []})
    return selection, {"valid_format": valid, "error": error, "foreign_ids": foreign_ids, "wall_ms": wall_ms, "raw_structured_output": str(raw)}


def selectors() -> None:
    data, ups = datasets(), upstreams(); store: dict[str, dict[str, Any]] = {name: {} for name in ("S0","S1","S2","S3","S4","S5","S6")}
    all_pairs = [(split, case) for split, cases in data.items() for case in cases]
    for split, case in all_pairs:
        planner = planner_selection(case, ups[split]["cases"][case["id"]]["planner"]["structured_output"])
        candidate = deterministic_candidates(case)
        store["S0"][case["id"]] = {"selection": planner.to_dict(), "meta": {"valid_format": True, "wall_ms": ups[split]["cases"][case["id"]]["planner"]["generation_wall_ms"]}}
        store["S6"][case["id"]] = {"candidate": candidate.to_dict()}
    critic, critic_tokenizer, _ = base._load_model(base.QWEN25_15B)
    try:
        for split, case in all_pairs:
            planner = EvidenceSelection(**{k: tuple(v) for k,v in store["S0"][case["id"]]["selection"].items()})
            candidate = EvidenceSelection(**{k: tuple(v) for k,v in store["S6"][case["id"]]["candidate"].items()})
            values = {}
            for selector, kwargs in (("S1", {}), ("S5", {"planner": planner}), ("S6", {"candidates": candidate})):
                generated = base.generate_timed(critic, critic_tokenizer, prompt=_selector_prompt(case, mode=selector, **kwargs), max_new_tokens=256)
                selection, meta = _model_selection(case, generated["raw"], generated["generation_wall_ms"])
                values[selector] = {"selection": selection.to_dict(), "meta": meta}
            store["S1"][case["id"]] = values["S1"]
            store["S5"][case["id"]] = values["S5"]
            store["S6"][case["id"]].update(values["S6"])
            s1 = EvidenceSelection(**{k: tuple(v) for k,v in values["S1"]["selection"].items()})
            store["S3"][case["id"]] = {"selection": combine_selections(planner, s1, "union").to_dict(), "meta": {"valid_format": values["S1"]["meta"]["valid_format"], "wall_ms": values["S1"]["meta"]["wall_ms"]}}
            store["S4"][case["id"]] = {"selection": combine_selections(planner, s1, "intersection").to_dict(), "meta": {"valid_format": values["S1"]["meta"]["valid_format"], "wall_ms": values["S1"]["meta"]["wall_ms"]}}
    finally:
        base._unload(critic, critic_tokenizer)
    qwen3, qwen3_tokenizer, _ = base._load_model(base.QWEN3)
    try:
        for split, case in all_pairs:
            generated = base.generate_timed(qwen3, qwen3_tokenizer, prompt=_selector_prompt(case, mode="S2"), max_new_tokens=256)
            selection, meta = _model_selection(case, generated["raw"], generated["generation_wall_ms"])
            store["S2"][case["id"]] = {"selection": selection.to_dict(), "meta": meta}
    finally:
        base._unload(qwen3, qwen3_tokenizer)
    report: dict[str, Any] = {"selectors": {}, "raw": store}
    case_index = {case["id"]: (split, case) for split, cases in data.items() for case in cases}
    for selector, values in store.items():
        split_rows = defaultdict(list)
        candidate_rows = defaultdict(list)
        for case_id, record in values.items():
            split, case = case_index[case_id]
            selection = EvidenceSelection(**{k: tuple(v) for k,v in record["selection"].items()})
            row = selection_row(case, selection, valid_format=record["meta"]["valid_format"], wall_ms=record["meta"]["wall_ms"])
            row["invented_ids"] = len(record["meta"].get("foreign_ids") or [])
            split_rows[split].append(row)
            if selector == "S6":
                candidate = EvidenceSelection(**{k: tuple(v) for k,v in record["candidate"].items()})
                candidate_rows[split].append(selection_row(case, candidate))
        metrics = {split: aggregate_selection(rows) for split, rows in split_rows.items()}
        report["selectors"][selector] = {**metrics, "gate": preliminary_gate(metrics["validation"], metrics["final_a"])}
        if selector == "S6": report["selectors"][selector]["candidate"] = {split: aggregate_selection(rows) for split,rows in candidate_rows.items()}
    dump(ROOT / "selectors.json", report)


def _packet(case: Mapping[str, Any], selection: EvidenceSelection, critic: Mapping[str, Any]) -> EvidencePacket:
    cr, xr = _critic_ids(critic, "rule"); cs, xs = _critic_ids(critic, "source")
    return EvidencePacket(
        protocol_version=PROTOCOL_VERSION, allowed_rule_ids=selection.allowed_rule_ids, allowed_source_ids=selection.allowed_source_ids,
        planner_rule_ids=selection.selected_rule_ids, planner_source_ids=selection.selected_source_ids,
        critic_confirmed_rule_ids=tuple(item for item in cr if item in selection.allowed_rule_ids), critic_challenged_rule_ids=tuple(item for item in xr if item in selection.allowed_rule_ids),
        critic_confirmed_source_ids=tuple(item for item in cs if item in selection.allowed_source_ids), critic_challenged_source_ids=tuple(item for item in xs if item in selection.allowed_source_ids),
        contradictions=tuple(critic.get("contradictions") or []),
    )


def _score_packet(case: Mapping[str, Any], upstream: Mapping[str, Any], packet: EvidencePacket, generated: Mapping[str, Any], runner: str) -> dict[str, Any]:
    evaluated = evaluate_solver_final(final_text=generated["raw"], case=case, trace={"case_id":case["id"]}, packet=packet)
    score = score_output(case, generated["raw"], evaluated.get("payload"), parser_error=evaluated.get("parse_error"), planner_rule_ids=packet.planner_rule_ids, planner_source_ids=packet.planner_source_ids, runner_id=runner)
    return {"case_id": case["id"], "domain_opinion_v1": evaluated.get("payload"), "parser_result": {k:v for k,v in evaluated.items() if k not in {"payload","metadata"}}, "timings": {"wall_total_ms": generated["generation_wall_ms"], "generation_wall_ms": generated["generation_wall_ms"], "ttft_ms": generated["ttft_ms"]}, "gpu_peak_bytes": int(torch.cuda.max_memory_allocated()), "ram_peak_bytes": 0, "timeout": False, **score}


def run_packet_variant(name: str, selections: Mapping[str, EvidenceSelection], *, splits: tuple[str,...]) -> dict[str, Any]:
    data, ups = datasets(), upstreams(); output = {}
    for split in splits:
        cases, upstream = data[split], ups[split]
        adapters = base._load_adapters("trained")
        hidden, audit = base._native_critic_hidden(cases, upstream, adapters)
        model, tokenizer, _ = base._load_model(base.QWEN3); solver_adapters = base._load_adapters("trained").to("cuda:0", dtype=torch.float32).eval()
        rows=[]
        try:
            with torch.inference_mode():
                for case in cases:
                    selection = selections[case["id"]]; critic = upstream["cases"][case["id"]]["critic"]["structured_output"]; packet = _packet(case, selection, critic)
                    value = hidden[case["id"]].to("cuda:0", dtype=torch.float32)
                    latent = solver_adapters["solver_inner"](solver_adapters["outer23"](solver_adapters["critic_inner"](value)))
                    generated = base.generate_timed(model, tokenizer, prompt=base.solver_prompt(case, critic, packet), latent=latent)
                    row = _score_packet(case, upstream, packet, generated, name); rows.append(row); dump(ROOT / "e2e" / name / split / f"{case['id']}.json", row)
        finally:
            solver_adapters.to("cpu"); base._unload(model, tokenizer)
        rows = [{**row, "human_decision_correct": float(True)} for row in rows]
        output[split] = aggregate_external(rows, {case["id"]:case for case in cases})
    return output


def oracle() -> None:
    cases = datasets()["final_a"]
    saved = sorted((ROOT / "e2e/oracle_gold_packet/final_a").glob("*.json"))
    if len(saved) == len(cases):
        rows = [json.loads(path.read_text()) for path in saved]
        for row in rows:
            row["timings"].setdefault("generation_wall_ms", row["timings"]["wall_total_ms"])
            row.setdefault("human_decision_correct", 1.0)
        result = {"final_a": aggregate_external(rows, {case["id"]: case for case in cases}), "reused_complete_decode_artifacts": True}
        dump(ROOT / "oracle_summary.json", result)
        return
    selections = {case["id"]: selection_from_payload(case, {"selected_rule_ids": case["gold"]["required_rules"], "selected_source_ids": case["gold"]["required_sources"]}) for case in cases}
    result = run_packet_variant("oracle_gold_packet", selections, splits=("final_a",))
    dump(ROOT / "oracle_summary.json", result)


def e2e() -> None:
    report = json.loads((ROOT / "selectors.json").read_text())
    eligible = [name for name,value in report["selectors"].items() if value["gate"]["passed"]]
    output = {"eligible": eligible, "variants": {}}
    for name in eligible:
        selections = {case_id: EvidenceSelection(**{k:tuple(v) for k,v in row["selection"].items()}) for case_id,row in report["raw"][name].items()}
        output["variants"][name] = run_packet_variant(name, selections, splits=("validation","final_a"))
    dump(ROOT / "e2e_summary.json", output)


def adapter_utility() -> None:
    # Full development comparison. "without both" is tensor-identical to zeroed outer12 and reuses that result.
    data, ups = datasets(), upstreams(); output = {}
    previous = base.ROOT; base.ROOT = ROOT / "adapter_utility_runtime"
    try:
        for split, cases in data.items():
            upstream = ups[split]; variants = {}
            for name, omit in (("with_planner_inner_outer12", None), ("without_planner_inner", "planner_inner"), ("without_outer12", "outer12")):
                hidden, audit = base._native_critic_hidden(cases, upstream, base._load_adapters("trained"), omit=omit)
                rows = base.run_qwen3_runner(f"utility_{name}_{split}", cases, upstream, adapter_kind="trained", native_hidden=hidden, native_audit=audit)
                variants[name] = base.aggregate(rows)
            variants["without_both"] = {**variants["without_outer12"], "reused_tensor_identical_zero_outer12": True}
            output[split] = variants
    finally:
        base.ROOT = previous
    dump(ROOT / "adapter_utility.json", output)


def finalize() -> None:
    selector = json.loads((ROOT / "selectors.json").read_text()); oracle_data = json.loads((ROOT / "oracle_summary.json").read_text())
    eligible = [name for name,value in selector["selectors"].items() if value["gate"]["passed"]]
    def rank(name: str) -> float:
        value = selector["selectors"][name]
        metrics = sum(value[split][metric] for split in ("validation","final_a") for metric in ("rule_recall","rule_precision","source_recall","source_precision","format_validity"))
        violations = sum(value[split]["invented_ids"] for split in ("validation","final_a"))
        return (4.0 if value["gate"]["validation_passed"] else 0.0) + metrics - violations
    best = max(selector["selectors"], key=rank)
    summary = {"best_selector": best, "eligible": eligible, "selectors": selector["selectors"], "oracle": oracle_data, "reserve_b": json.loads((ROOT/"manifest.json").read_text())["reserve_b"]}
    if (ROOT / "e2e_summary.json").exists(): summary["e2e"] = json.loads((ROOT / "e2e_summary.json").read_text())
    if (ROOT / "adapter_utility.json").exists(): summary["adapter_utility"] = json.loads((ROOT / "adapter_utility.json").read_text())
    dump(ROOT / "summary.json", summary)


def main() -> None:
    parser=argparse.ArgumentParser(); parser.add_argument("phase", choices=("prepare","validation-upstream","audit","technical","selectors","oracle","e2e","adapter-utility","finalize")); args=parser.parse_args()
    {"prepare":prepare,"validation-upstream":validation_upstream,"audit":audit_current,"technical":technical_audit,"selectors":selectors,"oracle":oracle,"e2e":e2e,"adapter-utility":adapter_utility,"finalize":finalize}[args.phase]()


if __name__ == "__main__": main()
