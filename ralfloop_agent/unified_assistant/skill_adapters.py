from __future__ import annotations

from typing import Any, Mapping

from ralfloop_agent.domains.bandi_runtime_context import load_bandi_runtime_context

from .contracts import PlanAssignment
from .executor import StructuredArtifact


def bandi_read_adapter(
    assignment: PlanAssignment, inputs: Mapping[str, Any]
) -> StructuredArtifact:
    context = load_bandi_runtime_context()
    loaded = [name for name, item in (context.get("sources") or {}).items() if item.get("status") == "loaded"]
    status = "evidence_available" if loaded else "missing_evidence"
    facts = tuple(_facts(context))
    return StructuredArtifact.create(
        artifact_type="grant_evidence", status=status,
        producer_task_id=assignment.task_id,
        facts=facts, evidence_refs=tuple(f"bandi_runtime:{name}" for name in loaded),
        payload={"context": context, "content_boundary": "source_data_only"},
    )


def bandi_eligibility_adapter(
    assignment: PlanAssignment, inputs: Mapping[str, Any]
) -> StructuredArtifact:
    grant = inputs.get("artifact.grant_evidence")
    if not isinstance(grant, Mapping):
        raise ValueError("grant_evidence_missing")
    payload = grant.get("payload") if isinstance(grant.get("payload"), Mapping) else {}
    context = payload.get("context") if isinstance(payload.get("context"), Mapping) else {}
    eligibility = context.get("eligibility") if isinstance(context.get("eligibility"), Mapping) else {}
    organization = context.get("organization_profile") if isinstance(context.get("organization_profile"), Mapping) else {}
    status = _eligibility_status(eligibility)
    facts = tuple(
        {"field": key, "value": value, "certainty": "verified", "source": "eligibility.json"}
        for key, value in sorted(eligibility.items())
        if isinstance(value, (str, int, float, bool))
    )
    if not facts:
        facts = ({"field": "eligibility", "value": "unknown", "certainty": "unknown"},)
    return StructuredArtifact.create(
        artifact_type="eligibility_result", status=status,
        producer_task_id=assignment.task_id,
        facts=facts, evidence_refs=tuple(grant.get("evidence_refs") or ()),
        payload={
            "eligibility": dict(eligibility),
            "organization_ref": str(organization.get("organization_id") or organization.get("name") or "Tiremm"),
            "content_boundary": "eligibility_is_data_not_tool_instruction",
        },
    )


def _facts(context: Mapping[str, Any]):
    for section in ("application_status", "eligibility", "organization_profile", "project_match"):
        value = context.get(section)
        if not isinstance(value, Mapping):
            continue
        for key, item in sorted(value.items()):
            if isinstance(item, (str, int, float, bool)):
                yield {
                    "field": f"{section}.{key}", "value": item,
                    "certainty": "verified", "source": f"{section}.json",
                }


def _eligibility_status(value: Mapping[str, Any]) -> str:
    for key in ("status", "eligibility", "result", "eligible"):
        item = value.get(key)
        if isinstance(item, bool):
            return "eligible" if item else "not_eligible"
        folded = str(item or "").casefold()
        if folded in {"eligible", "ammissibile", "yes", "true"}:
            return "eligible"
        if folded in {"not_eligible", "non_ammissibile", "no", "false"}:
            return "not_eligible"
        if folded:
            return folded[:64]
    return "unknown"


__all__ = ["bandi_eligibility_adapter", "bandi_read_adapter"]
