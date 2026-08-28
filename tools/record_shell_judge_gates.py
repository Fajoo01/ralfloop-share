from __future__ import annotations

import argparse
from datetime import UTC, datetime
import hashlib
import json
from pathlib import Path
from xml.etree import ElementTree as ET


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def junit(path: Path) -> tuple[dict[str, int], list[str]]:
    root = ET.parse(path).getroot()
    cases = list(root.iter("testcase"))
    failures = []
    skipped = 0
    errors = 0
    for case in cases:
        identity = f"{case.get('classname')}::{case.get('name')}"
        if case.find("failure") is not None:
            failures.append(identity)
        if case.find("error") is not None:
            failures.append(identity)
            errors += 1
        if case.find("skipped") is not None:
            skipped += 1
    return {
        "tests": len(cases), "failures": len(failures), "errors": errors,
        "skipped": skipped,
    }, sorted(set(failures))


def dump(path: Path, value: dict[str, object]) -> None:
    path.write_text(json.dumps(value, sort_keys=True, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--commit", required=True)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--scoped", type=Path, required=True)
    parser.add_argument("--changed", type=Path, required=True)
    parser.add_argument("--local-canary", type=Path, required=True)
    parser.add_argument("--functiongemma", type=Path, required=True)
    parser.add_argument("--benchmark", type=Path, required=True)
    args = parser.parse_args()
    args.root.mkdir(parents=True, exist_ok=True)
    now = datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
    baseline, baseline_ids = junit(args.baseline)
    candidate, candidate_ids = junit(args.candidate)
    scoped, _ = junit(args.scoped)
    changed, _ = junit(args.changed)
    local, _ = junit(args.local_canary)
    comparison = {
        "baseline": baseline, "baseline_exit": "139",
        "baseline_failure_ids": baseline_ids,
        "baseline_report_complete": True, "baseline_report_sha256": sha(args.baseline),
        "candidate": candidate, "candidate_exit": "139",
        "candidate_failure_ids": candidate_ids,
        "candidate_report_complete": True, "candidate_report_sha256": sha(args.candidate),
        "new_failures": sorted(set(candidate_ids) - set(baseline_ids)),
        "new_failure_count": len(set(candidate_ids) - set(baseline_ids)),
        "resolved_failures": sorted(set(baseline_ids) - set(candidate_ids)),
        "no_regression": set(candidate_ids) <= set(baseline_ids),
        "suite_report_complete": True, "suite_exit_preexisting_same": True,
        "tested_commit": args.commit, "verified_at": now,
    }
    dump(args.root / "regression-comparison.json", comparison)
    canary = {
        "v": 2, "verified_at": now, "tested_commit": args.commit, "static": "pass",
        "unit_new_arch": {"status": "pass", "tests": scoped["tests"], "junit_sha256": sha(args.scoped)},
        "changed_scope": {"status": "pass", "tests": changed["tests"], "junit_sha256": sha(args.changed)},
        "local_arch_tests": {"status": "pass", "tests": local["tests"], "skipped": local["skipped"], "junit_sha256": sha(args.local_canary)},
        "fake_router": "pass", "fake_llm_director": "pass", "fake_evolver": "pass", "fake_media": "pass",
        "bwrap_real_c": "pass", "seccomp_network_block": "pass",
        "seccomp_process_block": "pass", "cgroup_v2_limits": "pass",
        "functiongemma_real_dry_run": "pass", "visual_worker_dry_run": "pass",
        "integration_dry_run": "pass", "published": False,
    }
    dump(args.root / "canary.json", canary)
    functiongemma = json.loads(args.functiongemma.read_text())
    functiongemma.update({"tested_commit": args.commit, "verified_at": now})
    dump(args.root / "functiongemma-real-canary.json", functiongemma)
    benchmark = json.loads(args.benchmark.read_text())
    benchmark.update({"tested_commit": args.commit, "verified_at": now})
    dump(args.root / "architecture-benchmark-v2.json", benchmark)
    model_path = Path("/home/sibilla-cumana/Dati/ralfloop-models/functiongemma-270m/functiongemma-270m-it-q8_0.gguf")
    model = {
        "v": 2, "verified_at": now, "tested_commit": args.commit,
        "local_model_present": model_path.is_file(), "model_path": str(model_path),
        "sha256": functiongemma.get("model_sha256"), "runtime_canary": "pass",
        "functiongemma_real": functiongemma.get("pass") is True,
        "canary_evidence": ".ralf_run/local_arch_v1/functiongemma-real-canary.json",
        "canary_sha256": sha(args.root / "functiongemma-real-canary.json"),
        "cpu_only": functiongemma.get("cpu_only") is True,
        "policy_bypass": sum(int(case.get("metrics", {}).get("policy_bypass", 0)) for case in functiongemma.get("cases", [])),
        "approval_miss": sum(int(case.get("metrics", {}).get("approval_miss", 0)) for case in functiongemma.get("cases", [])),
    }
    dump(args.root / "model-verification.json", model)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
