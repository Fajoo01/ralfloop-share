from __future__ import annotations

from dataclasses import asdict, dataclass, field
import json
from typing import Any, Callable


REQUIRED_OUTPUT_FIELDS = {
    "domain_id",
    "question",
    "position",
    "supporting_arguments",
    "counterarguments",
    "rule_application",
    "evidence_used",
    "uncertainties",
    "alternative_interpretations",
    "recommendation",
    "confidence",
    "human_decision_required",
}
FORBIDDEN_ACTION_MARKERS = (
    "auto-approve",
    "auto_approve",
    "auto-execute",
    "auto_execute",
    "execute-approved",
    "execute_approved",
    "promote_domain",
    "run_domain_canary",
    "apply_domain_source_update",
)


@dataclass(frozen=True)
class DomainReasoningInput:
    domain_id: str
    domain_version: str
    question: str
    facts: tuple[dict[str, Any], ...]
    rules: tuple[dict[str, Any], ...]
    sources: tuple[dict[str, Any], ...]
    constraints: tuple[str, ...]
    known_contradictions: tuple[dict[str, Any], ...]
    required_output: dict[str, Any] = field(default_factory=dict)
    reason_codes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.question.strip():
            raise ValueError("domain_question_required")
        creation_review = "domain_creation_review" in self.reason_codes
        if (not self.domain_id.strip() or not self.domain_version.strip()) and not creation_review:
            raise ValueError("domain_required")
        if not self.rules and not creation_review:
            raise ValueError("domain_rules_required")
        if not self.sources and not creation_review:
            raise ValueError("domain_sources_required")

    @classmethod
    def from_domain(
        cls,
        domain: dict[str, Any],
        question: str,
        *,
        facts: dict[str, Any] | list[dict[str, Any]] | None = None,
        reason_codes: list[str] | tuple[str, ...] = (),
        constraints: list[str] | tuple[str, ...] = (),
    ) -> "DomainReasoningInput":
        manifest = dict(domain.get("manifest") or {})
        raw_facts = facts or {}
        if isinstance(raw_facts, dict):
            normalized_facts = tuple(
                {"fact_id": str(key), "statement": value, "kind": "fact"}
                for key, value in sorted(raw_facts.items())
            )
        else:
            normalized_facts = tuple(dict(item) for item in raw_facts)
        default_constraints = (
            "No external action.",
            "No implicit approval.",
            "Distinguish facts, rules, inferences, opinions, uncertainties and recommendations.",
            *[f"out_of_scope:{item}" for item in manifest.get("out_of_scope", [])],
        )
        return cls(
            domain_id=str(manifest.get("domain_id") or ""),
            domain_version=str(manifest.get("version") or ""),
            question=str(question),
            facts=normalized_facts,
            rules=tuple(dict(item) for item in domain.get("rules", [])),
            sources=tuple(dict(item) for item in domain.get("sources", [])),
            constraints=tuple(dict.fromkeys((*default_constraints, *[str(item) for item in constraints]))),
            known_contradictions=tuple(dict(item) for item in domain.get("conflicts", [])),
            required_output={"schema": "ralf-domain-opinion-v1", "fields": sorted(REQUIRED_OUTPUT_FIELDS)},
            reason_codes=tuple(dict.fromkeys(str(item) for item in reason_codes)),
        )

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        for key in ("facts", "rules", "sources", "constraints", "known_contradictions", "reason_codes"):
            data[key] = list(data[key])
        return data


def build_domain_reasoning_prompt(request: DomainReasoningInput) -> str:
    contract = {
        "domain_id": request.domain_id,
        "question": request.question,
        "position": "opinion, not fact",
        "supporting_arguments": [{"text": "...", "kind": "inference", "refs": ["fact_or_source_id"]}],
        "counterarguments": [{"text": "strongest contrary case", "kind": "inference", "refs": []}],
        "rule_application": [{"rule_id": "existing rule id", "inference": "application result"}],
        "evidence_used": [{"kind": "fact|source", "id": "existing id"}],
        "uncertainties": ["unresolved uncertainty"],
        "alternative_interpretations": ["credible alternative"],
        "recommendation": "conditional recommendation",
        "confidence": 0.0,
        "human_decision_required": False,
    }
    roles = {
        "planner": [
            "Identify issues and missing information.",
            "Build at least two alternative hypotheses.",
            "Select only supplied rules and sources.",
        ],
        "critic": [
            "Find contradictions, bias and logical gaps.",
            "Verify rule application.",
            "State the strongest counterargument.",
        ],
        "solver": [
            "Compare alternatives and preserve unresolved critic objections as uncertainties.",
            "Separate facts, rules, inferences, opinions and recommendations.",
            "Return only the JSON contract; do not expose chain of thought.",
        ],
    }
    return json.dumps(
        {
            "task": "domain_aware_reasoned_opinion",
            "roles": roles,
            "input": request.to_dict(),
            "output_contract": contract,
            "safety": ["No action", "No approval", "No domain creation or promotion", "Never invent rule or source IDs"],
        },
        ensure_ascii=False,
        sort_keys=True,
    )


def parse_domain_opinion(raw: Any) -> dict[str, Any] | None:
    if isinstance(raw, dict):
        return raw
    text = str(raw or "").strip()
    if text.startswith("```"):
        lines = text.splitlines()
        text = "\n".join(lines[1:-1]).strip() if len(lines) >= 3 else text
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end <= start:
            return None
        try:
            value = json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            return None
    return value if isinstance(value, dict) else None


def validate_domain_opinion(payload: Any, request: DomainReasoningInput) -> dict[str, Any]:
    errors: list[str] = []
    if not isinstance(payload, dict):
        return {"ok": False, "status": "recursive_output_invalid", "errors": ["output_not_object"]}
    missing = sorted(REQUIRED_OUTPUT_FIELDS - set(payload))
    if missing:
        errors.extend(f"missing:{field}" for field in missing)
    if payload.get("domain_id") != request.domain_id:
        errors.append("domain_id_invalid")
    if payload.get("question") != request.question:
        errors.append("question_mismatch")
    confidence = payload.get("confidence")
    if not isinstance(confidence, (int, float)) or isinstance(confidence, bool) or not 0 <= float(confidence) <= 1:
        errors.append("confidence_invalid")
    if not isinstance(payload.get("human_decision_required"), bool):
        errors.append("human_decision_required_invalid")
    for name in (
        "supporting_arguments",
        "counterarguments",
        "rule_application",
        "evidence_used",
        "uncertainties",
        "alternative_interpretations",
    ):
        if not isinstance(payload.get(name), list):
            errors.append(f"{name}_invalid")
    valid_rules = {str(item.get("rule_id")) for item in request.rules if item.get("rule_id")}
    valid_sources = {str(item.get("source_id")) for item in request.sources if item.get("source_id")}
    valid_facts = {str(item.get("fact_id")) for item in request.facts if item.get("fact_id")}
    for item in payload.get("rule_application", []) if isinstance(payload.get("rule_application"), list) else []:
        if not isinstance(item, dict) or str(item.get("rule_id")) not in valid_rules:
            errors.append("invented_rule")
    for item in payload.get("evidence_used", []) if isinstance(payload.get("evidence_used"), list) else []:
        if not isinstance(item, dict) or item.get("kind") not in {"fact", "source"}:
            errors.append("evidence_provenance_invalid")
            continue
        valid = valid_facts if item["kind"] == "fact" else valid_sources
        if str(item.get("id")) not in valid:
            errors.append("invented_fact" if item["kind"] == "fact" else "invented_source")
    for field_name in ("supporting_arguments", "counterarguments"):
        for item in payload.get(field_name, []) if isinstance(payload.get(field_name), list) else []:
            if not isinstance(item, dict) or item.get("kind") not in {"inference", "opinion"} or not str(item.get("text") or "").strip():
                errors.append(f"{field_name}_claim_type_invalid")
    if request.known_contradictions and not payload.get("counterarguments"):
        errors.append("critic_counterargument_missing")
    if request.known_contradictions and not payload.get("uncertainties"):
        errors.append("solver_ignored_unresolved_critic")
    serialized = json.dumps(payload, ensure_ascii=False).lower()
    if any(marker in serialized for marker in FORBIDDEN_ACTION_MARKERS):
        errors.append("forbidden_action_or_approval")
    return {
        "ok": not errors,
        "status": "valid" if not errors else "recursive_output_invalid",
        "errors": sorted(set(errors)),
        "rule_ids": sorted(valid_rules),
        "source_ids": sorted(valid_sources),
        "fact_ids": sorted(valid_facts),
    }


def execute_domain_opinion(
    request: DomainReasoningInput,
    executor: Callable[[dict[str, Any]], dict[str, Any]],
    *,
    backend: str = "recursive_mas_native",
) -> dict[str, Any]:
    runtime = executor(
        {
            "goal": build_domain_reasoning_prompt(request),
            "rounds": 2,
            "profile": "release_like",
            "domain_reasoning": True,
            "side_effect": False,
            "requires_human_confirmation": False,
        }
    )
    if not isinstance(runtime, dict) or not runtime.get("ok"):
        return {
            "ok": False,
            "status": str(runtime.get("status") or "recursive_failed") if isinstance(runtime, dict) else "recursive_failed",
            "selected_backend": backend,
            "external_action_executed": False,
        }
    opinion = parse_domain_opinion(runtime.get("answer"))
    validation = validate_domain_opinion(opinion, request)
    if not validation["ok"]:
        return {
            "ok": False,
            "status": "recursive_output_invalid",
            "selected_backend": backend,
            "validation": validation,
            "external_action_executed": False,
        }
    return {
        "ok": True,
        "status": "completed",
        "selected_backend": backend,
        "opinion": opinion,
        "validation": validation,
        "native_latent_verified": bool(runtime.get("native_latent_verified")),
        "duration_ms": runtime.get("duration_ms"),
        "memory": runtime.get("memory") or {},
        "external_action_executed": False,
    }
