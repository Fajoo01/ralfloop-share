from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import time
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ralfloop_agent.unified_assistant.contracts import AssistantFeatureFlags
from ralfloop_agent.unified_assistant.runtime import run_unified_telegram, unified_route_probe


DEFAULT_QUERY = "Cos'è una APS in Italia?"
AUTHORITY_HOSTS = (
    "normattiva.it",
    "gazzettaufficiale.it",
    "lavoro.gov.it",
)


def _host(url: str) -> str:
    return (urlparse(url).hostname or "").casefold().removeprefix("www.")


def _execution_artifact(metadata: dict) -> dict:
    execution = metadata.get("execution") or {}
    artifacts = execution.get("artifacts") or []
    for artifact in artifacts:
        if artifact.get("artifact_type") == "grounded_research":
            return dict(artifact)
    return {}


def run_case(query: str) -> dict:
    flags = AssistantFeatureFlags(unified_assistant=True)
    context = {
        "source": "ralf_terminal",
        "assistant_surface": "assistant_v1",
        "terminal_client": {"session_id": "benchmark-grounded-aps-20260921"},
    }
    route = unified_route_probe(query, context, flags_override=flags)
    started = time.monotonic()
    result = run_unified_telegram(query, context, flags_override=flags)
    wall_ms = int((time.monotonic() - started) * 1000)

    metadata = dict(result.get("metadata") or {})
    artifact = _execution_artifact(metadata)
    payload = dict(artifact.get("payload") or {})
    citations = [dict(item) for item in payload.get("citations") or [] if isinstance(item, dict)]
    citation_urls = [str(item.get("url") or "") for item in citations]
    authority_urls = [url for url in citation_urls if any(_host(url).endswith(host) for host in AUTHORITY_HOSTS)]
    response = str(result.get("final_answer") or result.get("response") or "")
    normalized = response.casefold().replace(" ", "")
    gates = {
        "route_is_research_deep": bool(route and route.get("skills_used") == ["research.deep"]),
        "runtime_ok": bool(result.get("ok")),
        "grounded_artifact_completed": artifact.get("status") == "completed",
        "citations_present": bool(citations),
        "authoritative_source_present": bool(authority_urls),
        "d_lgs_117_2017_present": (
            "117/2017" in normalized
            or "decretolegislativo3luglio2017,n.117" in normalized
            or "decretolegislativo3luglio2017n.117" in normalized
        ),
        "read_only_network": payload.get("network_mode") == "read_only",
        "no_approval_required": not bool(result.get("approval_required")),
    }
    return {
        "schema_version": 1,
        "benchmark": "assistant-v1-grounded-aps-quality",
        "query": query,
        "wall_ms": wall_ms,
        "route": route,
        "result_ok": bool(result.get("ok")),
        "response": response,
        "citations": citations,
        "authority_urls": authority_urls,
        "partial": payload.get("partial"),
        "errors": payload.get("errors") or [],
        "run_id": payload.get("run_id"),
        "trace_path": payload.get("trace_path"),
        "gates": gates,
        "pass": all(gates.values()),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Run grounded Assistant v1 APS quality canary")
    parser.add_argument("--query", default=DEFAULT_QUERY)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    report = run_case(args.query)
    rendered = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    return 0 if report["pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
