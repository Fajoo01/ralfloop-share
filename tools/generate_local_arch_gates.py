from __future__ import annotations

import argparse
import json
from pathlib import Path

try:
    from tools import deploy_local_arch_release as deploy
    from tools.deploy_local_arch_release import evaluate_quality_gate, meminfo, port_free
except ModuleNotFoundError:
    import deploy_local_arch_release as deploy
    from deploy_local_arch_release import evaluate_quality_gate, meminfo, port_free


def load(path: Path) -> dict[str, object]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{path}: expected a JSON object")
    return data


def generate(root: Path) -> dict[str, object]:
    comparison = load(root / "regression-comparison.json")
    model = load(root / "model-verification.json")
    canary = load(root / "canary.json")
    benchmark = load(root / "architecture-benchmark-v2.json")
    unit_new_arch = canary.get("unit_new_arch", {})
    profiles = benchmark.get("profiles", {})
    memory = meminfo()
    tested_commits = {
        str(item.get("tested_commit"))
        for item in (comparison, model, canary, benchmark)
        if item.get("tested_commit")
    }
    tested_commit = next(iter(tested_commits)) if len(tested_commits) == 1 else None

    baseline = comparison.get("baseline", {})
    candidate = comparison.get("candidate", {})
    baseline_ids = comparison.get("baseline_failure_ids", [])
    candidate_ids = comparison.get("candidate_failure_ids", [])
    new_failure_ids = comparison.get("new_failures", [])
    legacy_evidence = (
        comparison.get("no_regression") is True
        and comparison.get("new_failure_count") == 0
        and isinstance(candidate, dict)
        and candidate.get("errors") == 0
        and isinstance(baseline_ids, list)
        and isinstance(candidate_ids, list)
        and set(candidate_ids) <= set(baseline_ids)
        and comparison.get("suite_report_complete") is True
        and comparison.get("suite_exit_preexisting_same") is True
    )
    data: dict[str, object] = {
        "v": 2,
        "tested_commit": tested_commit,
        "scoped_tests": isinstance(unit_new_arch, dict) and unit_new_arch.get("status") == "pass",
        "scoped_test_count": unit_new_arch.get("tests") if isinstance(unit_new_arch, dict) else None,
        "sandbox": all(canary.get(key) == "pass" for key in ("bwrap_real_c", "seccomp_network_block", "seccomp_process_block", "cgroup_v2_limits")),
        "no_host_candidate_execution": canary.get("integration_dry_run") == "pass",
        "benchmark_minimum": isinstance(profiles, dict) and bool(profiles) and all(isinstance(value, dict) and value.get("failures") == 0 for value in profiles.values()),
        "no_regression": comparison.get("no_regression") is True,
        "new_failures": len(new_failure_ids) if isinstance(new_failure_ids, list) else None,
        "baseline_suite": baseline,
        "candidate_suite": candidate,
        "baseline_failure_ids": baseline_ids,
        "candidate_failure_ids": candidate_ids,
        "full_suite_clean": isinstance(candidate, dict) and candidate.get("failures") == 0 and candidate.get("errors") == 0,
        "preexisting_failures_only": legacy_evidence,
        "suite_report_complete": comparison.get("suite_report_complete") is True,
        "suite_exit_preexisting_same": comparison.get("suite_exit_preexisting_same") is True,
        "baseline_exit": comparison.get("baseline_exit"),
        "candidate_exit": comparison.get("candidate_exit"),
        "functiongemma_real": model.get("functiongemma_real") is True,
        "runtime_canary": model.get("runtime_canary"),
        "policy_bypass": model.get("policy_bypass"),
        "approval_miss": model.get("approval_miss"),
        "enough_ram": memory["MemAvailable"] >= 4096,
        "enough_swap": memory["SwapFree"] >= 512,
        "ram_available_mb": memory["MemAvailable"],
        "swap_free_mb": memory["SwapFree"],
        "functiongemma_endpoint": deploy.functiongemma_endpoint_check(),
        "manifest": "pending_release_build",
    }
    allowed, path, blockers = evaluate_quality_gate(data)
    data["preexisting_failures_only"] = allowed and path == "preexisting_failures_only"
    data["deploy_allowed"] = allowed
    data["quality_gate_path"] = path
    data["deploy_blockers"] = blockers
    return data


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("root", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    rendered = json.dumps(generate(args.root), sort_keys=True, indent=2) + "\n"
    if args.output:
        args.output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
