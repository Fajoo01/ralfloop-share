from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess
import tempfile


PRODUCTION = Path("/home/sibilla-cumana/ralfloop-production")
MODEL = Path("/home/sibilla-cumana/Dati/ralfloop-models/functiongemma-270m/functiongemma-270m-it-q8_0.gguf")


def preflight(release: Path) -> dict[str, object]:
    gate_allowed, gate_path, gate_blockers = gate_marker_details(release)
    checks: dict[str, object] = {
        "release_under_root": release.resolve().parent == (PRODUCTION / "releases").resolve(),
        "release_metadata": (release / "RELEASE.json").is_file(),
        "release_identity": release_identity(release),
        "manifest": verify_manifest(release),
        "functiongemma_model": MODEL.is_file(),
        "port_19104_free": port_free(19104),
        "glm_incompatible_idle": not process_match(("glm-run-machine", "colibri_glm")),
    }
    memory = meminfo()
    checks["ram_available_mb"] = memory["MemAvailable"]
    checks["swap_free_mb"] = memory["SwapFree"]
    checks["enough_ram"] = memory["MemAvailable"] >= 4096
    checks["enough_swap"] = memory["SwapFree"] >= 512
    checks["gate_marker"] = gate_allowed
    checks["quality_gate_path"] = gate_path
    checks["quality_gate_blockers"] = gate_blockers
    informational = {"ram_available_mb", "swap_free_mb", "quality_gate_path", "quality_gate_blockers", "allowed"}
    checks["allowed"] = all(value is True for key, value in checks.items() if key not in informational)
    return checks


def release_identity(release: Path) -> bool:
    metadata = release / "RELEASE.json"
    if not metadata.is_file():
        return False
    try:
        data = json.loads(metadata.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return isinstance(data, dict) and data.get("commit") == release.resolve().name


def verify_manifest(release: Path) -> bool:
    manifest = release / "MANIFEST.sha256"
    if not manifest.is_file():
        return False
    try:
        for line in manifest.read_text(encoding="utf-8").splitlines():
            digest, relative = line.split("  ", 1)
            target = release / relative
            if not target.is_file() or hashlib.sha256(target.read_bytes()).hexdigest() != digest:
                return False
    except (OSError, ValueError):
        return False
    return True


def gate_marker(release: Path) -> bool:
    return gate_marker_details(release)[0]


def gate_marker_details(release: Path) -> tuple[bool, str | None, list[str]]:
    marker = release / ".ralf_run/local_arch_v1/gates.json"
    if not marker.is_file():
        return False, None, ["gate_marker_missing"]
    try:
        data = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False, None, ["gate_marker_invalid"]
    return evaluate_quality_gate(data)


def evaluate_quality_gate(data: dict[str, object]) -> tuple[bool, str | None, list[str]]:
    """Evaluate an auditable, fail-closed full-clean or legacy-suite gate."""
    common = {
        "scoped_tests": data.get("scoped_tests") is True,
        "sandbox": data.get("sandbox") is True,
        "no_host_candidate_execution": data.get("no_host_candidate_execution") is True,
        "benchmark_minimum": data.get("benchmark_minimum") is True,
        "functiongemma_real": data.get("functiongemma_real") is True,
        "runtime_canary": data.get("runtime_canary") == "pass",
        "policy_bypass": data.get("policy_bypass") == 0,
        "approval_miss": data.get("approval_miss") == 0,
        "enough_ram": data.get("enough_ram") is True,
        "enough_swap": data.get("enough_swap") is True,
        "port_19104_free": data.get("port_19104_free") is True,
    }
    common_blockers = [name for name, passed in common.items() if not passed]
    if common_blockers:
        return False, None, common_blockers
    if data.get("full_suite_clean") is True:
        return True, "full_suite_clean", []

    baseline = data.get("baseline_suite")
    candidate = data.get("candidate_suite")
    baseline_ids = data.get("baseline_failure_ids")
    candidate_ids = data.get("candidate_failure_ids")
    structured = (
        isinstance(baseline, dict)
        and isinstance(candidate, dict)
        and isinstance(baseline_ids, list)
        and isinstance(candidate_ids, list)
        and all(isinstance(item, str) for item in baseline_ids + candidate_ids)
    )
    legacy = {
        "structured_failure_evidence": structured,
        "no_regression": data.get("no_regression") is True,
        "new_failures_zero": data.get("new_failures") == 0,
        "candidate_errors_zero": isinstance(candidate, dict) and candidate.get("errors") == 0,
        "suite_report_complete": data.get("suite_report_complete") is True,
        "suite_exit_preexisting_same": data.get("suite_exit_preexisting_same") is True,
    }
    if structured:
        baseline_set = set(baseline_ids)
        candidate_set = set(candidate_ids)
        legacy.update(
            {
                "unique_failure_identities": len(baseline_ids) == len(baseline_set)
                and len(candidate_ids) == len(candidate_set),
                "candidate_failures_subset_baseline": candidate_set <= baseline_set,
                "baseline_failure_count_matches": baseline.get("failures") == len(baseline_set),
                "candidate_failure_count_matches": candidate.get("failures") == len(candidate_set),
            }
        )
    blockers = [name for name, passed in legacy.items() if not passed]
    return (not blockers, "preexisting_failures_only" if not blockers else None, blockers)


def port_free(port: int) -> bool:
    with socket.socket() as sock:
        try:
            sock.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def process_match(needles: tuple[str, ...]) -> bool:
    output = subprocess.run(["ps", "-eo", "args="], check=True, text=True, capture_output=True).stdout.casefold()
    for line in output.splitlines():
        if "deploy_local_arch_release" in line:
            continue
        if any(needle.casefold() in line for needle in needles):
            return True
    return False


def meminfo() -> dict[str, int]:
    values = {}
    for line in Path("/proc/meminfo").read_text().splitlines():
        key, value = line.split(":", 1)
        values[key] = int(value.strip().split()[0]) // 1024
    return values


def atomic_link(link: Path, target: Path) -> None:
    temporary = link.with_name(f".{link.name}.tmp-{os.getpid()}")
    temporary.symlink_to(target.resolve())
    os.replace(temporary, link)


def publish(release: Path) -> dict[str, object]:
    checks = preflight(release)
    if not checks["allowed"]:
        return {"published": False, "checks": checks}
    old = (PRODUCTION / "current").resolve()
    atomic_link(PRODUCTION / "previous", old)
    atomic_link(PRODUCTION / "current", release)
    return {"published": True, "previous": str(old), "current": str(release.resolve()), "checks": checks}


def rollback_drill(release: Path) -> dict[str, object]:
    old = (PRODUCTION / "current").resolve()
    with tempfile.TemporaryDirectory(prefix="ralf-rollback-drill-") as raw:
        root = Path(raw)
        current = root / "current"
        previous = root / "previous"
        atomic_link(current, old)
        atomic_link(previous, old)
        atomic_link(current, release)
        atomic_link(current, previous.resolve())
        return {"ok": current.resolve() == old, "restored": str(current.resolve()), "target": str(old)}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("release", type=Path)
    parser.add_argument("--publish", action="store_true")
    parser.add_argument("--rollback-drill", action="store_true")
    args = parser.parse_args()
    result: dict[str, object] = {"preflight": preflight(args.release)}
    if args.rollback_drill:
        result["rollback_drill"] = rollback_drill(args.release)
    if args.publish:
        result["deploy"] = publish(args.release)
    print(json.dumps(result, sort_keys=True, indent=2))
    return 0 if result["preflight"]["allowed"] else 3


if __name__ == "__main__":
    raise SystemExit(main())
