from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .cli import answer_goal
from .deterministic_engine import DeterministicEngine
from .reasoning_router import DomainReasoningRouter
from .registry import DomainRegistry
from .resolver import DomainResolver


def infer_reason_codes(question: str) -> list[str]:
    text = question.lower()
    rules = (
        ("conflicting_sources", ("conflitt", "contradd")),
        ("strategic_assessment", ("strateg", "opportun", "valuta")),
        ("recommendation_required", ("raccomand", "consigli", "conviene")),
        ("incomplete_rules", ("incomplet", "insufficient", "manca")),
        ("evidence_synthesis", ("evidenz", "sintesi")),
        ("domain_validation", ("valid", "dominio")),
    )
    found = [reason for reason, markers in rules if any(marker in text for marker in markers)]
    return found or ["qualitative_judgment"]


def explain_routing(
    domain_id: str,
    question: str,
    *,
    registry: DomainRegistry | None = None,
    explicit_recursive: bool = False,
) -> dict[str, Any]:
    selected_registry = registry or DomainRegistry()
    resolution = DomainResolver(selected_registry).resolve(question, {"domain_id": domain_id}).to_dict()
    if resolution.get("status") != "resolved":
        return {
            "domain": domain_id,
            "reason_codes": ["domain_creation_required"],
            "selected_backend": "domain_creation_required",
            "why_recursive": None,
            "why_not_recursive": "domain_not_resolved",
        }
    domain = selected_registry.find_active(domain_id)
    if not domain:
        return {
            "domain": domain_id,
            "reason_codes": ["domain_creation_required"],
            "selected_backend": "domain_creation_required",
            "why_recursive": None,
            "why_not_recursive": "domain_not_resolved",
        }
    engine = DeterministicEngine()
    classification = engine.classify(domain, question)
    deterministic = engine.evaluate(domain, question)
    route = DomainReasoningRouter().select(
        domain_resolution=resolution,
        classification=classification,
        deterministic_result=deterministic,
        requested_reason_codes=infer_reason_codes(question),
        explicit_recursive=explicit_recursive,
    )
    return {"domain": domain_id, **route}


def reason_domain(
    domain_id: str,
    question: str,
    *,
    recursive: bool = False,
    registry: DomainRegistry | None = None,
    jury_controller: Any | None = None,
) -> dict[str, Any]:
    return answer_goal(
        question,
        domain_id,
        registry,
        jury_controller,
        explicit_recursive=recursive,
        requested_reason_codes=infer_reason_codes(question),
    )


def latest_benchmark_summary(root: str | Path = ".ralf_run/recursive_domain_reasoning_benchmark") -> dict[str, Any]:
    manifests = sorted(Path(root).glob("runs/*/manifest.json"))
    if not manifests:
        return {"status": "not_run", "benchmark": "recursive_domain_reasoning"}
    manifest = json.loads(manifests[-1].read_text(encoding="utf-8"))
    return {
        "status": "completed",
        "benchmark": "recursive_domain_reasoning",
        "artifact": str(manifests[-1].parent),
        "case_count": manifest.get("case_count"),
        "aggregates": manifest.get("aggregates"),
        "adoption": manifest.get("adoption"),
        "codex_is_judge": False,
    }
