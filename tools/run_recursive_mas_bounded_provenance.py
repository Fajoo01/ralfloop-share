from __future__ import annotations

import argparse
from collections import Counter
import copy
import hashlib
import inspect
import json
from pathlib import Path
import random
import statistics
import sys
import time
from typing import Any, Mapping, Sequence

import torch

REPO = Path("/home/sibilla-cumana/ralfloop_agent_scaffold")
sys.path.insert(0, str(REPO / "tools"))
import run_recursive_mas_provenance_selector_diagnostics as previous  # noqa: E402
import run_recursive_mas_qwen3_heldout_benchmark as base  # noqa: E402

from ralfloop_agent.domains.recursive_mas_bounded_provenance import (  # noqa: E402
    BoundedDecisions, SlotMapping, apply_uncertain_policy, batch_prompt, decision_agreement,
    itemwise_prompt, most_relevant_fact, parse_batch_bounded, parse_itemwise_bounded,
    selection_jaccard, stable_sha256,
)
from ralfloop_agent.domains.recursive_mas_domain_provenance import EvidencePacket  # noqa: E402
from ralfloop_agent.domains.recursive_mas_external_benchmark import aggregate_external  # noqa: E402
from ralfloop_agent.domains.recursive_mas_provenance_selector_diagnostics import (  # noqa: E402
    EvidenceSelection, aggregate_selection, deterministic_candidates, preliminary_gate,
    reserve_seal_audit, selection_from_payload, selection_row,
)

ROOT = REPO / ".ralf_run/recursive_domain_bounded_selector"
RESERVE_B = REPO / ".ralf_run/recursive_domain_external_holdout/reserve_b.jsonl"
RESERVE_SHA = "44e70370dd5f4322bd19a5e654f24b24c945522f9474c0c5d3a6332533e2ed6e"
SELECTORS = ("S7", "S8", "S9", "S10", "S11")
POLICIES = ("P1_KEEP", "P2_SECOND_PASS")
SEED = 64117
MODEL_META = {
    "qwen3": {"model": "Qwen/Qwen3-1.7B", "revision": "70d244cc86ccca08cf5af4e1e306ecf908b1ad5e", "snapshot": base.QWEN3},
    "critic": {"model": "Qwen2.5-1.5B-Instruct", "revision": "989aa7980e4cf806f80c7fef2b1adb7bc71aa306", "snapshot": base.QWEN25_15B},
}


def dump(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _cases(split: str) -> list[dict[str, Any]]:
    return previous.datasets()[split]


def _upstream(split: str) -> Mapping[str, Any]:
    return previous.upstreams()[split]


def _selection(case: Mapping[str, Any], mode: str) -> EvidenceSelection:
    if mode == "gold":
        rules, sources = case["gold"]["required_rules"], case["gold"]["required_sources"]
    elif mode == "allowed":
        rules = [item["rule_id"] for item in case["rules"]]
        sources = [item["source_id"] for item in case["sources"]]
    else:
        raise ValueError("oracle_packet_mode_invalid")
    return selection_from_payload(case, {"selected_rule_ids": rules, "selected_source_ids": sources})


def _oracle_packet(case: Mapping[str, Any], upstream: Mapping[str, Any], mode: str) -> EvidencePacket:
    critic = upstream["cases"][case["id"]]["critic"]["structured_output"]
    return previous._packet(case, _selection(case, mode), critic)


def _oracle_aggregate(rows: Sequence[Mapping[str, Any]], cases: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    result = base.aggregate(rows)
    result.update({
        "human_decision_correctness": statistics.mean(float(row.get("human_decision_correct", 0)) for row in rows),
        "solver_format_errors": sum(row.get("error_classification") == "format_error" for row in rows),
        "solver_semantic_errors": sum(row.get("error_classification") == "solver_semantic_error" for row in rows),
        "adapter_transfer_errors": sum(row.get("error_classification") == "adapter_transfer_error" for row in rows),
        "packet_selection_scored": False,
    })
    return result


def run_oracles() -> None:
    summary: dict[str, Any] = {"splits": {}}
    previous_root = base.ROOT; base.ROOT = ROOT / "oracle_runtime"
    try:
        for split in ("validation", "final_a"):
            cases, upstream = _cases(split), _upstream(split)
            hidden, hidden_audit = base._native_critic_hidden(cases, upstream, base._load_adapters("trained"))
            model, tokenizer, load_ms = base._load_model(base.QWEN3)
            adapters = base._load_adapters("trained").to("cuda:0", dtype=torch.float32).eval()
            rows: dict[str, list[dict[str, Any]]] = {name: [] for name in ("O0", "O1", "O2", "O3")}
            try:
                with torch.inference_mode():
                    for case in cases:
                        critic = upstream["cases"][case["id"]]["critic"]["structured_output"]
                        gold = _oracle_packet(case, upstream, "gold")
                        available = _oracle_packet(case, upstream, "allowed")
                        value = hidden[case["id"]].to("cuda:0", dtype=torch.float32)
                        latent = adapters["solver_inner"](adapters["outer23"](adapters["critic_inner"](value)))
                        specs = (("O0", gold, None), ("O1", gold, latent), ("O2", available, None), ("O3", available, latent))
                        case_rows = {}
                        for name, prompt_packet, latent_value in specs:
                            generated = base.generate_timed(model, tokenizer, prompt=base.solver_prompt(case, critic, prompt_packet), latent=latent_value)
                            row = previous._score_packet(case, upstream, gold, generated, name)
                            row["prompt_packet_role"] = "gold_selected" if name in {"O0", "O1"} else "evidence_available"
                            row["serialization_packet_role"] = "gold_reference_not_selection_scored"
                            row["human_decision_correct"] = float(
                                isinstance((row.get("domain_opinion_v1") or {}).get("human_decision_required"), bool)
                                and (row["domain_opinion_v1"]["human_decision_required"] == bool(case.get("human_decision_requirement", case["gold"].get("human_decision_required", case["gold"].get("conditional_required")))))
                            )
                            case_rows[name] = row
                        if case_rows["O0"]["semantic_complete"] and not case_rows["O1"]["semantic_complete"]:
                            case_rows["O1"]["error_classification"] = "adapter_transfer_error"
                        if case_rows["O2"]["semantic_complete"] and not case_rows["O3"]["semantic_complete"]:
                            case_rows["O3"]["error_classification"] = "adapter_transfer_error"
                        for name, row in case_rows.items():
                            rows[name].append(row); dump(ROOT / "oracles" / split / name / f"{case['id']}.json", row)
            finally:
                adapters.to("cpu"); base._unload(model, tokenizer)
            summary["splits"][split] = {name: _oracle_aggregate(values, cases) for name, values in rows.items()}
            summary["splits"][split]["hidden_audit"] = {"load_ms": hidden_audit["load_ms"], "gpu_peak_bytes": hidden_audit["gpu_peak_bytes"]}
        final = summary["splits"]["final_a"]
        o0, o1, o2, o3 = (final[name] for name in ("O0", "O1", "O2", "O3"))
        summary["classification"] = {
            "latent_bridge_generalization_insufficient": o0["schema_validity"] - o1["schema_validity"] >= .10,
            "solver_domain_generalization_insufficient": o0["semantic_complete_count"] < 32 and o1["semantic_complete_count"] < 32,
            "evidence_overload_sensitive": (o0["schema_validity"] - o2["schema_validity"] >= .10) or (o1["schema_validity"] - o3["schema_validity"] >= .10),
            "latent_bridge_adds_semantic_value": o1["semantic_complete_count"] > o0["semantic_complete_count"],
            "provenance_not_primary_bottleneck": (o1["semantic_complete_count"] - 22) / 22 < .10,
        }
        summary["gold_packet_improvement_vs_runner_d"] = (o1["semantic_complete_count"] - 22) / 22
    finally:
        base.ROOT = previous_root
    dump(ROOT / "oracle_summary.json", summary)


def _candidate_items(case: Mapping[str, Any], mapping: SlotMapping, order_mode: str) -> list[tuple[str, str, str, str]]:
    items = [
        *(('RULE', slot, real_id, str(row.get("statement") or "")) for (slot, real_id), row in zip(mapping.rule_slots, case["rules"], strict=True)),
        *(('SOURCE', slot, real_id, str(row.get("statement") or "")) for (slot, real_id), row in zip(mapping.source_slots, case["sources"], strict=True)),
    ]
    if order_mode == "reverse": return list(reversed(items))
    if order_mode == "random":
        random.Random(f"{SEED}:{case['id']}").shuffle(items)
    return items


def _raw_has_real_id(raw: str, mapping: SlotMapping) -> list[str]:
    return [identifier for identifier in mapping.to_dict().values() if identifier in str(raw)]


def _generate_item(model: Any, tokenizer: Any, case: Mapping[str, Any], kind: str, slot: str, text: str, *, binary: bool = False) -> dict[str, Any]:
    facts = [most_relevant_fact(case, text)] if binary else None
    prompt=itemwise_prompt(case,candidate_type=kind,slot=slot,text=text,fact_texts=facts,binary=binary)
    labels=("KEEP","DROP") if binary else ("KEEP","DROP","UNCERTAIN")
    prompt_ids=base._chat_ids(tokenizer,prompt); candidates=[];decision_lengths=[]
    for label in labels:
        decision_ids=tokenizer(label,add_special_tokens=False)["input_ids"]
        suffix=tokenizer(label+"\nEND",add_special_tokens=False)["input_ids"]
        candidates.append(prompt_ids+suffix);decision_lengths.append(len(decision_ids))
    max_len=max(map(len,candidates));pad=int(tokenizer.pad_token_id);input_ids=torch.full((len(candidates),max_len),pad,dtype=torch.long,device="cuda:0");mask=torch.zeros_like(input_ids)
    for index,ids in enumerate(candidates): input_ids[index,:len(ids)]=torch.tensor(ids,device="cuda:0");mask[index,:len(ids)]=1
    started=time.monotonic()
    with torch.inference_mode(): logits=model(input_ids=input_ids,attention_mask=mask,use_cache=False,return_dict=True).logits.float()
    scores=[]
    for index,length in enumerate(decision_lengths):
        start=len(prompt_ids);targets=input_ids[index,start:start+length];token_logits=logits[index,start-1:start+length-1]
        scores.append(float(torch.log_softmax(token_logits,dim=-1).gather(1,targets.unsqueeze(1)).mean().item()))
    torch.cuda.synchronize();wall=(time.monotonic()-started)*1000.0;decision=labels[max(range(len(labels)),key=lambda index:scores[index])];raw=decision+"\nEND"
    parsed=parse_itemwise_bounded(raw,binary=binary)
    return {"raw":raw,"decision":parsed,"valid":True,"error":None,"wall_ms":wall,"foreign_ids":[],"label_logprob":dict(zip(labels,scores,strict=True)),"classification_mode":"bounded_response_loglikelihood"}


def _generate_raw(model: Any, tokenizer: Any, cases: Sequence[Mapping[str, Any]], *, include_batch: bool, include_itemwise: bool, order_mode: str) -> dict[str, Any]:
    output = {}
    for case in cases:
        mapping = SlotMapping.from_case(case); record: dict[str, Any] = {"slot_mapping": mapping.to_dict(), "order_mode": order_mode}
        items = _candidate_items(case, mapping, order_mode)
        if include_batch:
            rule_order = [real_id for kind, _, real_id, _ in items if kind == "RULE"]
            source_order = [real_id for kind, _, real_id, _ in items if kind == "SOURCE"]
            generated = base.generate_timed(model, tokenizer, prompt=batch_prompt(case, mapping, rule_order=rule_order, source_order=source_order), max_new_tokens=128)
            try:
                decisions = parse_batch_bounded(generated["raw"], rule_count=len(mapping.rule_slots), source_count=len(mapping.source_slots)); valid=True; error=None
            except Exception as exc:
                decisions = BoundedDecisions(tuple("DROP" for _ in mapping.rule_slots), tuple("DROP" for _ in mapping.source_slots)); valid=False; error=f"{type(exc).__name__}:{exc}"
            batch = {"raw": generated["raw"], "rules": list(decisions.rules), "sources": list(decisions.sources), "valid": valid, "error": error, "wall_ms": generated["generation_wall_ms"], "foreign_ids": _raw_has_real_id(generated["raw"], mapping), "second_pass": {}}
            for kind, slots, values in (("RULE", mapping.rule_slots, decisions.rules), ("SOURCE", mapping.source_slots, decisions.sources)):
                text_by_id = {str(item[f"{kind.casefold()}_id"]): str(item.get("statement") or "") for item in case[f"{kind.casefold()}s"]}
                for (slot, real_id), value in zip(slots, values, strict=True):
                    if value == "UNCERTAIN": batch["second_pass"][slot] = _generate_item(model, tokenizer, case, kind, slot, text_by_id[real_id], binary=True)
            record["batch"] = batch
        if include_itemwise:
            decisions = {}
            for kind, slot, real_id, text in items:
                row = _generate_item(model, tokenizer, case, kind, slot, text)
                row["foreign_ids"] = _raw_has_real_id(row["raw"], mapping)
                row["second_pass"] = _generate_item(model, tokenizer, case, kind, slot, text, binary=True)
                decisions[slot] = row
            record["itemwise"] = decisions
        output[case["id"]] = record
    return output


def _shortlist_ids(case: Mapping[str, Any]) -> tuple[set[str], set[str]]:
    value = deterministic_candidates(case)
    return set(value.selected_rule_ids), set(value.selected_source_ids)


def _evaluate_record(selector: str, case: Mapping[str, Any], record: Mapping[str, Any], rule_policy: str, source_policy: str) -> tuple[dict[str, Any], dict[str, str]]:
    mapping = SlotMapping.from_case(case); shortlisted_rules, shortlisted_sources = _shortlist_ids(case)
    format_valid, foreign, wall, calls, invalid_slots = True, [], 0.0, 0, 0
    final_by_slot: dict[str, str] = {}
    if selector == "S7":
        source = record["batch"]; calls=1; wall=float(source["wall_ms"]); format_valid=bool(source["valid"]); foreign.extend(source["foreign_ids"])
        raw_rules, raw_sources = tuple(source["rules"]), tuple(source["sources"])
        for kind, slots, raw_values, policy in (("RULE", mapping.rule_slots, raw_rules, rule_policy), ("SOURCE", mapping.source_slots, raw_sources, source_policy)):
            resolutions=[]
            if policy == "P2_SECOND_PASS":
                for (slot,_), value in zip(slots, raw_values, strict=True):
                    if value == "UNCERTAIN":
                        second=source["second_pass"].get(slot) or {"decision":"DROP","valid":False,"wall_ms":0,"foreign_ids":[]}
                        resolutions.append(second["decision"]); calls+=1; wall+=float(second["wall_ms"]); format_valid &= bool(second["valid"]); foreign.extend(second.get("foreign_ids") or [])
            resolved=apply_uncertain_policy(raw_values, policy, second_pass=resolutions)
            final_by_slot.update({slot:decision for (slot,_),decision in zip(slots,resolved,strict=True)})
    else:
        source = record["itemwise"]
        for kind, slots, policy, shortlisted in (("RULE", mapping.rule_slots, rule_policy, shortlisted_rules), ("SOURCE", mapping.source_slots, source_policy, shortlisted_sources)):
            for slot, real_id in slots:
                if selector in {"S10","S11"} and real_id not in shortlisted:
                    final_by_slot[slot]="DROP"; continue
                row=source[slot]; calls+=1; wall+=float(row["wall_ms"]); format_valid &= bool(row["valid"]); foreign.extend(row.get("foreign_ids") or [])
                decision=row["decision"]
                if decision == "UNCERTAIN":
                    if policy == "P1_KEEP": decision="KEEP"
                    else:
                        second=row.get("second_pass") or {"decision":"DROP","valid":False,"wall_ms":0,"foreign_ids":[]}
                        decision=second["decision"]; calls+=1; wall+=float(second["wall_ms"]); format_valid &= bool(second["valid"]); foreign.extend(second.get("foreign_ids") or [])
                final_by_slot[slot]=decision
    rule_values=tuple(final_by_slot[slot] for slot,_ in mapping.rule_slots); source_values=tuple(final_by_slot[slot] for slot,_ in mapping.source_slots)
    selection=mapping.selection(rule_values,source_values); row=selection_row(case,selection,valid_format=format_valid,wall_ms=wall)
    row.update({"foreign_ids_generated": len(set(foreign)), "invented_ids": len(set(foreign)), "invalid_slots_generated": invalid_slots, "invalid_slots_accepted": 0, "model_calls": calls, "rule_policy":rule_policy,"source_policy":source_policy,"slot_mapping_hash":stable_sha256(mapping.to_dict())})
    decisions_by_id={mapping.real_id(slot):value for slot,value in final_by_slot.items()}
    return row,decisions_by_id


def _aggregate(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    value=aggregate_selection(rows)
    value.update({
        "foreign_ids_generated":sum(int(row["foreign_ids_generated"]) for row in rows),
        "invalid_slots_generated":sum(int(row["invalid_slots_generated"]) for row in rows),
        "invalid_slots_accepted":sum(int(row["invalid_slots_accepted"]) for row in rows),
        "model_calls_per_case":statistics.mean(float(row["model_calls"]) for row in rows) if rows else 0.0,
    })
    return value


def _policy_options(selector: str, cases: Sequence[Mapping[str, Any]], raw: Mapping[str, Any]) -> dict[str, Any]:
    options={}
    for rule_policy in POLICIES:
        for source_policy in POLICIES:
            rows=[_evaluate_record(selector,case,raw[case["id"]],rule_policy,source_policy)[0] for case in cases]
            key=f"rules={rule_policy};sources={source_policy}"; options[key]={"rule_policy":rule_policy,"source_policy":source_policy,"metrics":_aggregate(rows),"rows":rows}
    return options


def _policy_score(record: Mapping[str, Any]) -> float:
    m=record["metrics"]
    gate=preliminary_gate(m,m)["passed"]
    return (100 if gate else 0)+4*(m["rule_recall"]+m["source_recall"])+2*(m["rule_precision"]+m["source_precision"])-3*(m["missing_evidence_rate"]+m["overselection_rate"])+m["format_validity"]-10*m["invented_ids"]


def run_validation_selectors() -> None:
    cases=_cases("validation")
    qwen,qtok,_=base._load_model(base.QWEN3)
    try: qraw=_generate_raw(qwen,qtok,cases,include_batch=True,include_itemwise=True,order_mode="original")
    finally: base._unload(qwen,qtok)
    critic,ctok,_=base._load_model(base.QWEN25_15B)
    try: craw=_generate_raw(critic,ctok,cases,include_batch=False,include_itemwise=True,order_mode="original")
    finally: base._unload(critic,ctok)
    dump(ROOT/"selector_validation_raw.json",{"qwen3":qraw,"critic":craw})
    variants={"S7":qraw,"S8":qraw,"S9":craw,"S10":qraw,"S11":craw}; summary={}
    for selector,raw in variants.items():
        options=_policy_options(selector,cases,raw); chosen=max(options,key=lambda key:_policy_score(options[key]))
        summary[selector]={"options":options,"chosen_policy":chosen,"chosen":options[chosen]}
        for option in summary[selector]["options"].values(): option.pop("rows",None)
        summary[selector]["chosen"].pop("rows",None)
    dump(ROOT/"selector_validation_summary.json",summary)


def _selected_configuration(validation: Mapping[str,Any]) -> str:
    return max(SELECTORS,key=lambda name:_policy_score(validation[name]["chosen"]))


def _config(selector: str, validation: Mapping[str,Any]) -> tuple[str,str]:
    chosen=validation[selector]["chosen"]
    return chosen["rule_policy"],chosen["source_policy"]


def _model_for_selector(selector: str) -> str:
    return "qwen3" if selector in {"S7","S8","S10"} else "critic"


def _order_raw(selector: str,cases:Sequence[Mapping[str,Any]],order_mode:str) -> dict[str,Any]:
    model_name=_model_for_selector(selector); model,tokenizer,_=base._load_model(MODEL_META[model_name]["snapshot"])
    try: return _generate_raw(model,tokenizer,cases,include_batch=selector=="S7",include_itemwise=selector!="S7",order_mode=order_mode)
    finally: base._unload(model,tokenizer)


def _shared_order_raw(cases: Sequence[Mapping[str, Any]], order_mode: str) -> dict[str, Any]:
    qwen, qtok, _ = base._load_model(base.QWEN3)
    try:
        qraw = _generate_raw(qwen, qtok, cases, include_batch=True, include_itemwise=True, order_mode=order_mode)
    finally:
        base._unload(qwen, qtok)
    critic, ctok, _ = base._load_model(base.QWEN25_15B)
    try:
        craw = _generate_raw(critic, ctok, cases, include_batch=False, include_itemwise=True, order_mode=order_mode)
    finally:
        base._unload(critic, ctok)
    return {"qwen3": qraw, "critic": craw}


def validation_order_and_freeze() -> None:
    validation=json.loads((ROOT/"selector_validation_summary.json").read_text()); raw=json.loads((ROOT/"selector_validation_raw.json").read_text()); cases=_cases("validation")
    reverse_all=_shared_order_raw(cases,"reverse"); randomized_all=_shared_order_raw(cases,"random"); order_summary={}
    for selector in SELECTORS:
        rule_policy,source_policy=_config(selector,validation); original=raw[_model_for_selector(selector)]
        reverse=reverse_all[_model_for_selector(selector)]; randomized=randomized_all[_model_for_selector(selector)]
        agreements=[];distances=[];mapping_ok=True
        for case in cases:
            _,one=_evaluate_record(selector,case,original[case["id"]],rule_policy,source_policy)
            original_sel=SlotMapping.from_case(case).selection(tuple(one[real] for _,real in SlotMapping.from_case(case).rule_slots),tuple(one[real] for _,real in SlotMapping.from_case(case).source_slots))
            for candidate_raw in (reverse,randomized):
                _,other=_evaluate_record(selector,case,candidate_raw[case["id"]],rule_policy,source_policy)
                other_sel=SlotMapping.from_case(case).selection(tuple(other[real] for _,real in SlotMapping.from_case(case).rule_slots),tuple(other[real] for _,real in SlotMapping.from_case(case).source_slots))
                agreements.append(decision_agreement(one,other)); distances.append(selection_jaccard(original_sel,other_sel)); mapping_ok &= original[case["id"]]["slot_mapping"]==candidate_raw[case["id"]]["slot_mapping"]
        order_summary[selector]={"decision_agreement":statistics.mean(agreements),"jaccard_distance_mean":statistics.mean(distances),"slot_mapping_consistent":mapping_ok,"gate":statistics.mean(agreements)>=.95 and statistics.mean(distances)<=.05 and mapping_ok}
        dump(ROOT/"order_validation"/f"{selector}.json",{"reverse":reverse,"random":randomized})
    selected=_selected_configuration(validation)
    source_file=REPO/"ralfloop_agent/domains/recursive_mas_bounded_provenance.py"
    manifest={
        "protocol":"bounded_provenance_selector_freeze_v1","frozen":True,"created_at":time.strftime("%Y-%m-%dT%H:%M:%SZ",time.gmtime()),"git_commit":_git_head(),
        "prompt_hash":stable_sha256({"batch":inspect.getsource(batch_prompt),"itemwise":inspect.getsource(itemwise_prompt)}),
        "parser_hash":sha256_file(source_file),"slot_mapper_hash":stable_sha256(inspect.getsource(SlotMapping)),
        "selected_configuration":selected,"policies":{name:validation[name]["chosen_policy"] for name in SELECTORS},
        "uncertain_policy":{"rule":_config(selected,validation)[0],"source":_config(selected,validation)[1]},
        "model":MODEL_META[_model_for_selector(selected)],"decoding":{"enable_thinking":False,"temperature":0,"do_sample":False,"batch_max_new_tokens":128,"itemwise_mode":"bounded_response_loglikelihood","itemwise_candidates":["KEEP\\nEND","DROP\\nEND","UNCERTAIN\\nEND"]},
        "validation_metrics":validation[selected]["chosen"]["metrics"],"validation_order":order_summary[selected],"all_order_metrics":order_summary,
        "source_sha256":sha256_file(source_file),"reserve_b_semantically_read":False,"reserve_b_executed":False,
    }
    dump(ROOT/"selector_frozen_manifest.json",manifest); dump(ROOT/"order_validation_summary.json",order_summary)


def _git_head() -> str:
    import subprocess
    return subprocess.check_output(["git","rev-parse","HEAD"],cwd=REPO,text=True).strip()


def _verify_freeze() -> Mapping[str,Any]:
    manifest=json.loads((ROOT/"selector_frozen_manifest.json").read_text()); source=REPO/"ralfloop_agent/domains/recursive_mas_bounded_provenance.py"
    if not manifest.get("frozen") or manifest["source_sha256"]!=sha256_file(source) or manifest["prompt_hash"]!=stable_sha256({"batch":inspect.getsource(batch_prompt),"itemwise":inspect.getsource(itemwise_prompt)}):
        raise RuntimeError("bounded_selector_freeze_mismatch")
    return manifest


def run_final_selectors() -> None:
    _verify_freeze(); cases=_cases("final_a")
    qwen,qtok,_=base._load_model(base.QWEN3)
    try: qraw=_generate_raw(qwen,qtok,cases,include_batch=True,include_itemwise=True,order_mode="original")
    finally: base._unload(qwen,qtok)
    critic,ctok,_=base._load_model(base.QWEN25_15B)
    try: craw=_generate_raw(critic,ctok,cases,include_batch=False,include_itemwise=True,order_mode="original")
    finally: base._unload(critic,ctok)
    dump(ROOT/"selector_final_raw.json",{"qwen3":qraw,"critic":craw})
    validation=json.loads((ROOT/"selector_validation_summary.json").read_text()); summary={}
    for selector in SELECTORS:
        rp,sp=_config(selector,validation); raw=qraw if _model_for_selector(selector)=="qwen3" else craw
        rows=[_evaluate_record(selector,case,raw[case["id"]],rp,sp)[0] for case in cases]
        summary[selector]={"rule_policy":rp,"source_policy":sp,"metrics":_aggregate(rows),"rows":rows}
    dump(ROOT/"selector_final_summary.json",summary)


def final_order() -> None:
    _verify_freeze(); validation=json.loads((ROOT/"selector_validation_summary.json").read_text())
    cases=sorted(_cases("final_a"),key=lambda case:hashlib.sha256(str(case["id"]).encode()).hexdigest())[:12]
    original_all=json.loads((ROOT/"selector_final_raw.json").read_text()); reverse_all=_shared_order_raw(cases,"reverse"); randomized_all=_shared_order_raw(cases,"random"); summaries={}
    for selector in SELECTORS:
        rp,sp=_config(selector,validation); model_name=_model_for_selector(selector); original=original_all[model_name]; reverse=reverse_all[model_name]; randomized=randomized_all[model_name]
        agreements=[];distances=[];mapping_ok=True
        for case in cases:
            _,one=_evaluate_record(selector,case,original[case["id"]],rp,sp); mapping=SlotMapping.from_case(case)
            one_sel=mapping.selection(tuple(one[real] for _,real in mapping.rule_slots),tuple(one[real] for _,real in mapping.source_slots))
            for raw in (reverse,randomized):
                _,other=_evaluate_record(selector,case,raw[case["id"]],rp,sp); other_sel=mapping.selection(tuple(other[real] for _,real in mapping.rule_slots),tuple(other[real] for _,real in mapping.source_slots))
                agreements.append(decision_agreement(one,other));distances.append(selection_jaccard(one_sel,other_sel));mapping_ok &= original[case["id"]]["slot_mapping"]==raw[case["id"]]["slot_mapping"]
        summaries[selector]={"case_count":len(cases),"decision_agreement":statistics.mean(agreements),"jaccard_distance_mean":statistics.mean(distances),"slot_mapping_consistent":mapping_ok,"gate":statistics.mean(agreements)>=.95 and statistics.mean(distances)<=.05 and mapping_ok}
    dump(ROOT/"order_final.json",{"summaries":summaries,"reverse":reverse_all,"random":randomized_all})


def _development_e2e_gate(metrics: Mapping[str, Any]) -> bool:
    return bool(
        metrics["semantic_complete_count"] >= 32 and metrics["schema_valid_count"] >= 34
        and metrics["rule_recall"] >= .875 and metrics["source_recall"] >= .875
        and metrics["contradiction_recall"] >= .90 and metrics["counterargument_coverage"] >= .90
        and metrics["uncertainty_presence"] >= .90 and metrics["recommendation_presence"] == 1.0
        and metrics["recommendation_condition_correctness"] >= .90
        and metrics["invented_rule_ids"] == 0 and metrics["invented_source_ids"] == 0
        and metrics["safety_violations"] == 0 and metrics["approval_violations"] == 0
    )


def run_e2e() -> None:
    _verify_freeze(); validation=json.loads((ROOT/"selector_validation_summary.json").read_text()); final=json.loads((ROOT/"selector_final_summary.json").read_text()); order_v=json.loads((ROOT/"order_validation_summary.json").read_text()); order_f=json.loads((ROOT/"order_final.json").read_text())["summaries"]
    eligible=[]
    for name in SELECTORS:
        gate=preliminary_gate(validation[name]["chosen"]["metrics"],final[name]["metrics"])
        if gate["passed"] and order_v[name]["gate"] and order_f[name]["gate"]: eligible.append(name)
    if not eligible:
        dump(ROOT/"e2e_summary.json",{"executed":False,"eligible":[],"reason":"no_selector_preliminary_gate"}); return
    cases=_cases("final_a");upstream=_upstream("final_a");raw_all=json.loads((ROOT/"selector_final_raw.json").read_text())
    previous_root=base.ROOT;base.ROOT=ROOT/"e2e_runtime"
    try:
        hidden,audit=base._native_critic_hidden(cases,upstream,base._load_adapters("trained"),omit="outer12")
        model,tokenizer,_=base._load_model(base.QWEN3);adapters=base._load_adapters("trained").to("cuda:0",dtype=torch.float32).eval();variants={}
        try:
            for name in eligible:
                rp,sp=_config(name,validation);raw=raw_all[_model_for_selector(name)];rows=[]
                with torch.inference_mode():
                    for case in cases:
                        selection_row_value,_=_evaluate_record(name,case,raw[case["id"]],rp,sp)
                        selection=selection_from_payload(case,{"selected_rule_ids":selection_row_value["selected_rule_ids"],"selected_source_ids":selection_row_value["selected_source_ids"]})
                        critic=upstream["cases"][case["id"]]["critic"]["structured_output"];packet=previous._packet(case,selection,critic)
                        value=hidden[case["id"]].to("cuda:0",dtype=torch.float32);latent=adapters["solver_inner"](adapters["outer23"](adapters["critic_inner"](value)))
                        generated=base.generate_timed(model,tokenizer,prompt=base.solver_prompt(case,critic,packet),latent=latent)
                        row=previous._score_packet(case,upstream,packet,generated,f"bounded_{name}");rows.append(row);dump(ROOT/"e2e"/name/f"{case['id']}.json",row)
                metrics=base.aggregate(rows);variants[name]={"metrics":metrics,"gate":_development_e2e_gate(metrics)}
        finally:
            adapters.to("cpu");base._unload(model,tokenizer)
    finally:
        base.ROOT=previous_root
    dump(ROOT/"e2e_summary.json",{"executed":True,"eligible":eligible,"variants":variants,"candidate_path":["bounded_selector","critic_inner","outer23","solver_inner","qwen3"],"planner_inner_used":False,"outer12_used":False,"native_hidden_audit":audit})


def finalize() -> None:
    manifest=_verify_freeze(); validation=json.loads((ROOT/"selector_validation_summary.json").read_text()); final=json.loads((ROOT/"selector_final_summary.json").read_text()); order_v=json.loads((ROOT/"order_validation_summary.json").read_text()); order_f=json.loads((ROOT/"order_final.json").read_text())["summaries"]
    selectors={};eligible=[]
    for name in SELECTORS:
        vm=validation[name]["chosen"]["metrics"];fm=final[name]["metrics"]; gate=preliminary_gate(vm,fm); stability=order_v[name]["gate"] and order_f[name]["gate"]
        gate["order_stability_passed"]=stability;gate["passed"] &= stability
        selectors[name]={"validation":vm,"final_a":fm,"gate":gate,"policy":validation[name]["chosen_policy"]}
        if gate["passed"]: eligible.append(name)
    reserve=reserve_seal_audit(RESERVE_B,RESERVE_SHA)
    e2e=json.loads((ROOT/"e2e_summary.json").read_text()) if (ROOT/"e2e_summary.json").exists() else {"executed":False,"eligible":[]}
    candidate=any(value.get("gate") for value in e2e.get("variants",{}).values())
    summary={"selected_configuration":manifest["selected_configuration"],"selectors":selectors,"eligible":eligible,"candidate_for_reserve_b":candidate,"end_to_end":e2e,"reserve_b":reserve,"training_executed":False}
    dump(ROOT/"summary.json",summary)


def main() -> None:
    parser=argparse.ArgumentParser();parser.add_argument("phase",choices=("oracles","validation","freeze","final","final-order","e2e","finalize"));args=parser.parse_args()
    {"oracles":run_oracles,"validation":run_validation_selectors,"freeze":validation_order_and_freeze,"final":run_final_selectors,"final-order":final_order,"e2e":run_e2e,"finalize":finalize}[args.phase]()


if __name__=="__main__": main()
