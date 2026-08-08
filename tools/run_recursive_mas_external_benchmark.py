from __future__ import annotations

import argparse
import json
from pathlib import Path
import shutil
from typing import Any, Mapping

import torch

import run_recursive_mas_qwen3_heldout_benchmark as base
from ralfloop_agent.domains.recursive_mas_external_benchmark import (
    aggregate_external,
    blind_outputs_external,
    enrich_score,
    external_gate,
    load_external_cases,
    planner_metrics,
    select_infrastructure_canary,
    trained_exceeds_direct,
    verify_dataset_freeze,
    verify_runner_freeze,
)
from ralfloop_agent.domains.recursive_mas_qwen3_heldout_benchmark import RUNNERS, audit_checkpoints


REPO = Path("/home/sibilla-cumana/ralfloop_agent_scaffold")
ROOT = REPO / ".ralf_run/recursive_domain_external_holdout"
DATA = REPO / "ralfloop_agent/domains/data/recursive_domain_external_holdout"
FINAL_A = DATA / "final_a.jsonl"
RESERVE_B = DATA / "reserve_b.jsonl"
DATASET_MANIFEST = DATA / "dataset_manifest.json"
RUNNER_MANIFEST = ROOT / "runner_frozen_manifest.json"
SEED = 20260715


def dump(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def preflight() -> dict[str, Any]:
    dataset_manifest = json.loads(DATASET_MANIFEST.read_text())
    runner_manifest = json.loads(RUNNER_MANIFEST.read_text())
    verify_dataset_freeze(FINAL_A, dataset_manifest, "FINAL_A")
    verify_dataset_freeze(RESERVE_B, dataset_manifest, "RESERVE_B")
    verify_runner_freeze(REPO, runner_manifest)
    cases = load_external_cases(FINAL_A)
    profile = json.loads(base.PROFILE.read_text())
    final_manifest = json.loads((base.FINAL / "manifest.json").read_text())
    checkpoints = audit_checkpoints(profile, final_manifest, REPO)
    expected = runner_manifest["checkpoint_sha256"]
    actual = {name: row["sha256_actual"] for name, row in checkpoints["checkpoints"].items()}
    if actual != expected:
        raise RuntimeError("external_checkpoint_freeze_mismatch")
    result = {
        "final_a_count": len(cases),
        "reserve_b_count": int(dataset_manifest["RESERVE_B"]["case_count"]),
        "reserve_b_executed": False,
        "runner_frozen": True,
        "dataset_frozen": True,
        "checkpoints": checkpoints,
        "runner_order": list(RUNNERS),
        "fresh_adapter_seed": 42,
        "outer31_present": False,
        "training_executed": False,
    }
    dump(ROOT / "preflight.json", result)
    return result


def infrastructure_canary() -> dict[str, Any]:
    preflight()
    cases = select_infrastructure_canary(load_external_cases(FINAL_A))
    canary_root = ROOT / "infrastructure_canary_tmp"
    base.ROOT = canary_root
    try:
        upstream = base.run_upstream(cases)
        trained_hidden, trained_audit = base._native_critic_hidden(cases, upstream, base._load_adapters("trained"))
        rows = base.run_qwen3_runner(
            "D_recursive_trained", cases, upstream, adapter_kind="trained",
            native_hidden=trained_hidden, native_audit=trained_audit,
        )
        passed = all(not row.get("timeout") and float(row["timings"]["ttft_ms"]) <= float(row["timings"]["wall_total_ms"]) for row in rows)
        result = {
            "passed": passed,
            "case_ids": [case["id"] for case in cases],
            "model_loaded": True,
            "checkpoints_loaded": True,
            "parser_operational": all("parser_result" in row for row in rows),
            "infrastructure_errors": sum(bool(row.get("timeout")) for row in rows),
            "semantic_quality_inspected": False,
            "outputs_deleted": True,
        }
    finally:
        shutil.rmtree(canary_root, ignore_errors=True)
    dump(ROOT / "infrastructure_canary.json", result)
    if not result["passed"]:
        raise RuntimeError("external_infrastructure_canary_failed")
    return result


def _enrich_rows(
    rows: list[dict[str, Any]], cases: list[Mapping[str, Any]], upstream: Mapping[str, Any] | None
) -> list[dict[str, Any]]:
    by_id = {case["id"]: case for case in cases}
    return [enrich_score(row, by_id[row["case_id"]], upstream) for row in rows]


def _write_rows(rows: list[Mapping[str, Any]]) -> None:
    for row in rows:
        dump(ROOT / "runs" / str(row["runner_id"]) / f"{row['case_id']}.json", row)


def run_outer23_ablation(
    cases: list[Mapping[str, Any]], upstream: Mapping[str, Any]
) -> dict[str, Any]:
    selected = select_infrastructure_canary(cases)
    hidden, audit = base._native_critic_hidden(selected, upstream, base._load_adapters("trained"))
    model, tokenizer, load_total = base._load_model(base.QWEN3)
    adapters = base._load_adapters("trained").to("cuda:0", dtype=torch.float32).eval()
    rows = []
    try:
        with torch.inference_mode():
            for case in selected:
                packet = base._packet(case, upstream)
                latent = base._ablation_latent(adapters, hidden[case["id"]], "outer23")
                generated = base.generate_timed(
                    model, tokenizer,
                    prompt=base.solver_prompt(case, upstream["cases"][case["id"]]["critic"]["structured_output"], packet),
                    latent=latent,
                )
                row = base._score_solver_row(
                    case, upstream, generated, "D4_without_outer23",
                    load_ms=load_total / len(selected), native_wall_ms=float(audit["cases"][case["id"]]["wall_ms"]),
                    gpu_peak_bytes=int(torch.cuda.max_memory_allocated()),
                )
                rows.append(enrich_score(row, case, upstream))
                dump(ROOT / "ablation/D4_without_outer23" / f"{case['id']}.json", rows[-1])
    finally:
        adapters.to("cpu")
        base._unload(model, tokenizer)
    return {"executed": True, "case_ids": [case["id"] for case in selected], "aggregate": aggregate_external(rows, {case["id"]: case for case in selected})}


def run_full() -> dict[str, Any]:
    preflight_data = preflight()
    canary = json.loads((ROOT / "infrastructure_canary.json").read_text())
    if not canary.get("passed") or not canary.get("outputs_deleted"):
        raise RuntimeError("external_infrastructure_canary_required")
    cases = load_external_cases(FINAL_A)
    case_by_id = {case["id"]: case for case in cases}
    base.ROOT = ROOT
    results: dict[str, list[dict[str, Any]]] = {}
    results["A_single_domain"], single_runtime = base.run_single_domain(cases)
    results["A_single_domain"] = _enrich_rows(results["A_single_domain"], cases, None)
    _write_rows(results["A_single_domain"])
    upstream = base.run_upstream(cases)
    results["B_qwen3_direct"] = _enrich_rows(
        base.run_qwen3_runner("B_qwen3_direct", cases, upstream, adapter_kind=None), cases, upstream
    )
    _write_rows(results["B_qwen3_direct"])
    fresh_hidden, fresh_audit = base._native_critic_hidden(cases, upstream, base._load_adapters("fresh"))
    dump(ROOT / "runs/C_recursive_fresh/native_hidden_audit.json", fresh_audit)
    results["C_recursive_fresh"] = _enrich_rows(
        base.run_qwen3_runner("C_recursive_fresh", cases, upstream, adapter_kind="fresh", native_hidden=fresh_hidden, native_audit=fresh_audit),
        cases, upstream,
    )
    _write_rows(results["C_recursive_fresh"])
    trained_hidden, trained_audit = base._native_critic_hidden(cases, upstream, base._load_adapters("trained"))
    dump(ROOT / "runs/D_recursive_trained/native_hidden_audit.json", trained_audit)
    results["D_recursive_trained"] = _enrich_rows(
        base.run_qwen3_runner("D_recursive_trained", cases, upstream, adapter_kind="trained", native_hidden=trained_hidden, native_audit=trained_audit),
        cases, upstream,
    )
    _write_rows(results["D_recursive_trained"])
    aggregates = {name: aggregate_external(rows, case_by_id) for name, rows in results.items()}
    gate = external_gate(aggregates)
    planner = planner_metrics(cases, upstream)
    ablation = run_outer23_ablation(cases, upstream) if trained_exceeds_direct(aggregates) else {"executed": False, "reason": "runner_d_did_not_exceed_runner_b"}
    blind, mapping = blind_outputs_external(results, SEED)
    dump(ROOT / "blind_review.json", {
        "rubric": ["position_strength", "counterargument_quality", "contradiction_handling", "uncertainty_clarity", "recommendation_usefulness", "balance"],
        "automatic_review": False, "model_judge_used": False, "candidates": blind,
    })
    mapping_path = ROOT / "blind_mapping.json"
    dump(mapping_path, mapping)
    mapping_path.chmod(0o600)
    summary = {
        "aggregates": aggregates,
        "gate": gate,
        "planner": planner,
        "ablation": ablation,
        "preflight": preflight_data,
        "infrastructure_canary": canary,
        "single_runtime": single_runtime,
        "reserve_b_executed": False,
        "runner_order": list(RUNNERS),
        "training_executed": False,
        "planner_refinement_executed": False,
        "production_authorized": False,
        "human_review_executed": False,
        "model_judge_used": False,
    }
    dump(ROOT / "summary.json", summary)
    return summary


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("phase", choices=("preflight", "canary", "run", "all"))
    parser.add_argument("--dataset", type=Path, default=FINAL_A)
    args = parser.parse_args()
    if args.dataset.resolve() != FINAL_A.resolve():
        load_external_cases(args.dataset)
        raise RuntimeError("external_dataset_path_not_frozen_final_a")
    if args.phase == "preflight":
        result = preflight()
    elif args.phase == "canary":
        result = infrastructure_canary()
    elif args.phase == "run":
        result = run_full()
    else:
        preflight()
        infrastructure_canary()
        result = run_full()
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
