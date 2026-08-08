from __future__ import annotations

import argparse
import json
from pathlib import Path
import xml.etree.ElementTree as ET


def failures(path: Path) -> tuple[dict[str, int], set[str]]:
    root = ET.parse(path).getroot()
    suite = root if root.tag == "testsuite" else next(iter(root.findall("testsuite")), root)
    counts = {name: int(suite.attrib.get(name, 0)) for name in ("tests", "failures", "errors", "skipped")}
    failed = set()
    for case in root.iter("testcase"):
        if case.find("failure") is not None or case.find("error") is not None:
            failed.add(f"{case.attrib.get('classname')}::{case.attrib.get('name')}")
    return counts, failed


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("baseline", type=Path)
    parser.add_argument("candidate", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    baseline_counts, baseline_failed = failures(args.baseline)
    candidate_counts, candidate_failed = failures(args.candidate)
    result = {
        "v": 1,
        "baseline": baseline_counts,
        "candidate": candidate_counts,
        "new_failures": sorted(candidate_failed - baseline_failed),
        "resolved_failures": sorted(baseline_failed - candidate_failed),
        "no_regression": not (candidate_failed - baseline_failed),
    }
    rendered = json.dumps(result, sort_keys=True, indent=2) + "\n"
    if args.output:
        args.output.write_text(rendered)
    print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
