from __future__ import annotations

import json
from pathlib import Path
import re
from typing import Any, Mapping

from ralfloop_agent.domains.bandi_runtime_context import load_bandi_runtime_context
from ralfloop_agent.model_tools import ModelToolManager, ModelToolRegistry

from .contracts import PlanAssignment
from .executor import StructuredArtifact


_BANDO_REF_RE = re.compile(r"\bRL[A-Z]\d{8,16}\b", re.I)
_GROUNDED_RESEARCH_PROFILES = (
    Path(__file__).resolve().parents[2] / "config" / "grounded_research_profiles.json"
)


def _grounded_research_profile(profile_id: str) -> dict[str, Any]:
    if not profile_id:
        return {}
    payload = json.loads(_GROUNDED_RESEARCH_PROFILES.read_text(encoding="utf-8"))
    if payload.get("schema_version") != 1 or not isinstance(payload.get("profiles"), dict):
        raise ValueError("grounded_research_profiles_invalid")
    profile = payload["profiles"].get(profile_id)
    if not isinstance(profile, dict):
        raise ValueError("grounded_research_profile_unknown")
    allowed = {
        "domains", "seed_urls", "max_steps", "max_sources",
        "require_primary", "min_opened_sources",
    }
    if set(profile) - allowed:
        raise ValueError("grounded_research_profile_keys_invalid")
    return dict(profile)


def bandi_read_adapter(
    assignment: PlanAssignment, inputs: Mapping[str, Any]
) -> StructuredArtifact:
    requested_ref = _grant_ref_from_inputs(inputs)
    context = (
        load_bandi_runtime_context(bando_ref=requested_ref)
        if requested_ref else load_bandi_runtime_context()
    )
    loaded = [name for name, item in (context.get("sources") or {}).items() if item.get("status") == "loaded"]
    status = "evidence_available" if loaded else "missing_evidence"
    facts = tuple(_facts(context))
    return StructuredArtifact.create(
        artifact_type="grant_evidence", status=status,
        producer_task_id=assignment.task_id,
        facts=facts, evidence_refs=tuple(
            f"bandi_runtime:{requested_ref or 'legacy_default'}:{name}" for name in loaded
        ),
        payload={
            "context": context,
            "requested_grant_ref": requested_ref,
            "content_boundary": "source_data_only",
        },
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


def research_deep_adapter(
    assignment: PlanAssignment,
    inputs: Mapping[str, Any],
    *,
    manager: ModelToolManager | None = None,
) -> StructuredArtifact:
    active_manager = manager or ModelToolManager(ModelToolRegistry.load())
    arguments = dict(assignment.arguments)
    query = str(arguments.get("query") or assignment.objective).strip()
    profile = _grounded_research_profile(str(arguments.get("profile") or "").strip())
    payload: dict[str, Any] = {"query": query, **profile}
    for key in (
        "domains", "seed_urls", "max_steps", "max_sources",
        "require_primary", "min_opened_sources",
    ):
        if key in arguments:
            payload[key] = arguments[key]

    envelope = active_manager.invoke("deep_web_research_agentcpm_v1", payload)
    if not envelope.ok:
        return StructuredArtifact.create(
            artifact_type="grounded_research",
            status="unavailable",
            producer_task_id=assignment.task_id,
            payload={
                "message": f"Ricerca grounded non disponibile: {envelope.error_type or 'unknown_error'}.",
                "tool_id": envelope.tool_id,
                "error_type": envelope.error_type,
                "warnings": list(envelope.warnings),
                "content_boundary": "tool_failure_is_data",
            },
        )

    output = dict(envelope.output)
    citations = [item for item in output.get("citations") or () if isinstance(item, Mapping)]
    claims = [item for item in output.get("claims") or () if isinstance(item, Mapping)]
    answer = str(output.get("answer") or "").strip()
    read_only = str(output.get("network_mode") or "") == "read_only"
    if not answer or not citations or not read_only:
        return StructuredArtifact.create(
            artifact_type="grounded_research",
            status="unavailable",
            producer_task_id=assignment.task_id,
            payload={
                "message": "Ricerca grounded senza evidenza citabile sufficiente; risposta non pubblicata.",
                "run_id": output.get("run_id"),
                "partial": bool(output.get("partial")),
                "errors": list(output.get("errors") or ()),
                "content_boundary": "ungrounded_output_not_published",
            },
        )

    refs = tuple(
        dict.fromkeys(
            str(item.get("url") or item.get("source_id") or "")
            for item in citations
            if item.get("url") or item.get("source_id")
        )
    )
    source_lines = [
        f"[{str(item.get('source_id') or '?')}] {str(item.get('title') or '').strip()} — {str(item.get('url') or '').strip()}"
        for item in citations
    ]
    message = answer + ("\n\nFonti:\n" + "\n".join(source_lines) if source_lines else "")
    return StructuredArtifact.create(
        artifact_type="grounded_research",
        status="completed",
        producer_task_id=assignment.task_id,
        facts=tuple(dict(item) for item in claims[:64]),
        evidence_refs=refs[:64],
        payload={
            "message": message,
            "answer": answer,
            "citations": [dict(item) for item in citations],
            "sources": list(output.get("sources") or ()),
            "partial": bool(output.get("partial")),
            "errors": list(output.get("errors") or ()),
            "run_id": output.get("run_id"),
            "trace_path": output.get("trace_path"),
            "network_mode": output.get("network_mode"),
            "tool_id": envelope.tool_id,
            "duration_ms": envelope.duration_ms,
            "content_boundary": "web_evidence_is_data",
        },
    )


def _grant_ref_from_inputs(inputs: Mapping[str, Any]) -> str:
    """Extract only a bounded Regione-style call id from upstream source evidence."""
    for key, value in inputs.items():
        if not str(key).startswith("artifact.") or not isinstance(value, Mapping):
            continue
        try:
            text = json.dumps(value, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            continue
        match = _BANDO_REF_RE.search(text)
        if match:
            return match.group(0).upper()
    return ""


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


__all__ = ["bandi_eligibility_adapter", "bandi_read_adapter", "research_deep_adapter"]
