from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from openshell_backend.assistant_v1_api import AssistantV1Request, _model_lane
from ralfloop_agent.unified_assistant.contracts import AssistantFeatureFlags
from ralfloop_agent.unified_assistant.runtime import unified_route_probe


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CASES = ROOT / "benchmarks" / "assistant-v1-real-world-cases-v1.json"


def load_cases(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict) or not isinstance(payload.get("cases"), list):
        raise ValueError("benchmark payload must contain a cases list")
    return payload


def evaluate_case(case: dict[str, Any]) -> dict[str, Any]:
    message = str(case["message"])
    allow_tools = bool(case.get("allow_tools", True))
    route = None
    if allow_tools:
        route = unified_route_probe(
            message,
            {
                "source": "ralf_terminal",
                "assistant_surface": "assistant_v1",
                "session_id": f"benchmark-{case['id']}",
            },
            flags_override=AssistantFeatureFlags(unified_assistant=True),
        )

    if route is not None:
        observed_route = "unified"
        observed_domains = list(route.get("domains") or [])
        observed_skills = list(route.get("skills_used") or [])
        model_lane = None
        routing_reason = None
    else:
        request = AssistantV1Request(
            message=message,
            allow_tools=allow_tools,
            mode=str(case.get("mode") or "auto"),
        )
        model_lane, _model, routing_reason = _model_lane(request)
        observed_route = model_lane
        observed_domains = []
        observed_skills = []

    expected_route = str(case["expected_route"])
    expected_domain = case.get("expected_domain")
    expected_skill = case.get("expected_skill")
    route_ok = observed_route == expected_route
    domain_ok = expected_domain is None or expected_domain in observed_domains
    skill_ok = expected_skill is None or expected_skill in observed_skills
    return {
        "id": case["id"],
        "category": case["category"],
        "expected_route": expected_route,
        "observed_route": observed_route,
        "expected_domain": expected_domain,
        "observed_domains": observed_domains,
        "expected_skill": expected_skill,
        "observed_skills": observed_skills,
        "model_lane": model_lane,
        "routing_reason": routing_reason,
        "pass": route_ok and domain_ok and skill_ok,
    }


def run_benchmark(path: Path) -> dict[str, Any]:
    payload = load_cases(path)
    results = [evaluate_case(case) for case in payload["cases"]]
    categories = sorted({str(row["category"]) for row in results})
    passed = sum(bool(row["pass"]) for row in results)
    return {
        "schema_version": 1,
        "benchmark": payload.get("name"),
        "source": str(path),
        "safety": "route-only; no Unified runner, MCP call, or external write executed",
        "cases": len(results),
        "passed": passed,
        "failed": len(results) - passed,
        "pass_rate": round(passed / len(results), 4) if results else 0.0,
        "categories": categories,
        "results": results,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Benchmark Bot-tazzi Assistant v1 routing without side effects")
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    report = run_benchmark(args.cases)
    rendered = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    return 0 if report["failed"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
