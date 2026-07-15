from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import subprocess
from typing import Any

from ralfloop_agent.domains.recursive_mas_domain_dataset import deterministic_splits, generate_dataset
from ralfloop_agent.domains.recursive_mas_qwen3_final_benchmark import (
    OBSERVED_12,
    contamination_audit,
    freeze_manifest,
    partition_final_test,
)
from ralfloop_agent.domains.recursive_mas_qwen3_heldout_benchmark import (
    PRIOR_CANARY_IDS,
    audit_checkpoints,
    sha256_file,
    stable_sha256,
)


REPO = Path("/home/sibilla-cumana/ralfloop_agent_scaffold")
ROOT = REPO / ".ralf_run/recursive_domain_qwen3_final_benchmark"
PROFILE = REPO / "ralfloop_agent/domains/recursive_mas_domain_qwen3_solver_v1.json"
FINAL = REPO / ".ralf_run/recursive_domain_qwen3_micro_overfit/final"
LIMITED = REPO / ".ralf_run/recursive_domain_qwen3_heldout_benchmark"
QWEN25_3B = Path("/home/sibilla-cumana/RecursiveMAS/.hf-domain-instruct-v1/hub/models--Qwen--Qwen2.5-3B-Instruct/snapshots/aa8e72537993ba99e69dfaafa59ed015b17504d1")
QWEN25_15B = Path("/home/sibilla-cumana/RecursiveMAS/.hf-domain-instruct-v1/hub/models--Qwen--Qwen2.5-1.5B-Instruct/snapshots/989aa7980e4cf806f80c7fef2b1adb7bc71aa306")
QWEN3 = Path("/home/sibilla-cumana/RecursiveMAS/.hf-domain-qwen3-solver-v1/hub/models--Qwen--Qwen3-1.7B/snapshots/70d244cc86ccca08cf5af4e1e306ecf908b1ad5e")


def dump(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _combined_hash(paths: list[Path]) -> str:
    digest = hashlib.sha256()
    for path in sorted(paths):
        digest.update(str(path).encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


def _tokenizer_hash(snapshot: Path) -> str:
    names = ("tokenizer.json", "tokenizer_config.json", "special_tokens_map.json", "vocab.json", "merges.txt")
    paths = [snapshot / name for name in names if (snapshot / name).is_file()]
    if not paths:
        raise RuntimeError(f"tokenizer_files_missing:{snapshot}")
    return _combined_hash(paths)


def _observed_sources() -> dict[str, list[str]]:
    sources: dict[str, list[str]] = {}
    diagnosis = REPO / ".ralf_run/recursive_domain_reasoning_diagnosis/cases"
    if diagnosis.is_dir():
        for path in diagnosis.iterdir():
            if path.is_dir():
                sources.setdefault(path.name, []).append("diagnostic_run")
    canary = REPO / ".ralf_run/recursive_domain_qwen3_solver/protocol_canary/summary.json"
    if canary.is_file():
        for case_id in json.loads(canary.read_text()).get("case_order", []):
            sources.setdefault(str(case_id), []).append("qwen3_protocol_canary")
    for case_id in PRIOR_CANARY_IDS:
        sources.setdefault(case_id, []).append("prior_canary_registry")
    for case_id in OBSERVED_12:
        sources.setdefault(case_id, []).append("limited_heldout_benchmark")
    return sources


def prepare() -> dict[str, Any]:
    cases, _ = generate_dataset()
    splits = deterministic_splits(cases)
    partitions = partition_final_test(cases, splits)
    by_id = {str(case["id"]): case for case in cases}
    observed_sources = _observed_sources()
    reference_ids = set(splits["train"]) | set(splits["validation"]) | set(observed_sources)
    references = [by_id[case_id] for case_id in sorted(reference_ids) if case_id in by_id]
    contamination = contamination_audit(
        partitions["untouched_24"], references, observed_sources=observed_sources
    )
    profile = json.loads(PROFILE.read_text())
    final_manifest = json.loads((FINAL / "manifest.json").read_text())
    checkpoints = audit_checkpoints(profile, final_manifest, REPO)
    component_files = {
        "prompt_hash": [REPO / "ralfloop_agent/domains/recursive_mas_domain_provenance.py"],
        "parser_hash": [REPO / "ralfloop_agent/domains/recursive_mas_domain_serialization.py", REPO / "ralfloop_agent/domains/recursive_mas_qwen3_solver_eval.py"],
        "serializer_hash": [REPO / "ralfloop_agent/domains/recursive_mas_domain_serialization.py", REPO / "ralfloop_agent/domains/recursive_mas_domain_provenance.py"],
        "evidence_packet_schema_hash": [REPO / "ralfloop_agent/domains/recursive_mas_domain_provenance.py"],
        "runner_hash": [REPO / "tools/run_recursive_mas_qwen3_heldout_benchmark.py", Path(__file__)],
    }
    fields = {
        "git_commit": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=REPO, text=True).strip(),
        "dataset_hash": stable_sha256(cases),
        "split_hash": stable_sha256(splits),
        "checkpoint_hashes": {name: row["sha256_actual"] for name, row in checkpoints["checkpoints"].items()},
        "checkpoint_audit": checkpoints,
        "model_revisions": {
            "planner": "aa8e72537993ba99e69dfaafa59ed015b17504d1",
            "critic": "989aa7980e4cf806f80c7fef2b1adb7bc71aa306",
            "solver": "70d244cc86ccca08cf5af4e1e306ecf908b1ad5e",
        },
        "tokenizer_hashes": {
            "planner": _tokenizer_hash(QWEN25_3B),
            "critic": _tokenizer_hash(QWEN25_15B),
            "solver": _tokenizer_hash(QWEN3),
        },
        **{name: _combined_hash(paths) for name, paths in component_files.items()},
        "partitions": {name: [case["id"] for case in rows] for name, rows in partitions.items()},
        "partition_hashes": {name: stable_sha256([case["id"] for case in rows]) for name, rows in partitions.items()},
        "contamination_audit": contamination,
        "outer31_present": False,
        "profile_enabled": bool(profile.get("enabled")),
        "runtime_registered": False,
        "feature_flag_expected": "0",
        "execution_blocked": bool(contamination["contaminated"]),
        "classification": contamination["classification"],
    }
    manifest = freeze_manifest(fields)
    dump(ROOT / "frozen_manifest.json", manifest)
    dump(ROOT / "partitions.json", {name: rows for name, rows in fields["partitions"].items()})
    state = {
        "phase": "blocked_before_execution" if contamination["contaminated"] else "manifest_frozen",
        "contaminated": contamination["contaminated"],
        "classification": contamination["classification"],
        "runners_executed": [],
        "training_executed": False,
        "planner_refinement_executed": False,
        "production_authorized": False,
    }
    dump(ROOT / "state.json", state)
    return {"manifest": manifest, "state": state}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("phase", choices=("prepare", "run"))
    args = parser.parse_args()
    result = prepare()
    if args.phase == "run":
        if result["state"]["contaminated"]:
            raise RuntimeError("final_holdout_contamination")
        raise RuntimeError("final_runner_requires_clean_primary_partition")
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
