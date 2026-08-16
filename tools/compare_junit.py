from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import xml.etree.ElementTree as ET


def report(path: Path) -> tuple[dict[str, int], set[str], bool]:
    root = ET.parse(path).getroot()
    suites = [root] if root.tag == "testsuite" else list(root.findall("testsuite"))
    if not suites:
        raise ValueError(f"{path}: no testsuite in JUnit report")
    counts = {
        name: sum(int(suite.attrib.get(name, 0)) for suite in suites)
        for name in ("tests", "failures", "errors", "skipped")
    }
    cases = list(root.iter("testcase"))
    identities = [f"{case.attrib.get('classname')}::{case.attrib.get('name')}" for case in cases]
    failed = {
        identity
        for identity, case in zip(identities, cases)
        if case.find("failure") is not None or case.find("error") is not None
    }
    actual_failures = sum(case.find("failure") is not None for case in cases)
    actual_errors = sum(case.find("error") is not None for case in cases)
    actual_skipped = sum(case.find("skipped") is not None for case in cases)
    complete = (
        counts["tests"] == len(cases)
        and counts["failures"] == actual_failures
        and counts["errors"] == actual_errors
        and counts["skipped"] == actual_skipped
        and len(identities) == len(set(identities))
    )
    return counts, failed, complete


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("baseline", type=Path)
    parser.add_argument("candidate", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--baseline-exit", required=True)
    parser.add_argument("--candidate-exit", required=True)
    args = parser.parse_args()
    baseline_counts, baseline_failed, baseline_complete = report(args.baseline)
    candidate_counts, candidate_failed, candidate_complete = report(args.candidate)
    new_failures = candidate_failed - baseline_failed
    result = {
        "v": 2,
        "baseline": baseline_counts,
        "candidate": candidate_counts,
        "baseline_failure_ids": sorted(baseline_failed),
        "candidate_failure_ids": sorted(candidate_failed),
        "new_failures": sorted(new_failures),
        "new_failure_count": len(new_failures),
        "resolved_failures": sorted(baseline_failed - candidate_failed),
        "no_regression": not new_failures,
        "baseline_report_sha256": hashlib.sha256(args.baseline.read_bytes()).hexdigest(),
        "candidate_report_sha256": hashlib.sha256(args.candidate.read_bytes()).hexdigest(),
        "baseline_report_complete": baseline_complete,
        "candidate_report_complete": candidate_complete,
        "suite_report_complete": baseline_complete and candidate_complete,
        "baseline_exit": args.baseline_exit,
        "candidate_exit": args.candidate_exit,
        "suite_exit_preexisting_same": args.baseline_exit == args.candidate_exit,
    }
    rendered = json.dumps(result, sort_keys=True, indent=2) + "\n"
    if args.output:
        args.output.write_text(rendered)
    print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
