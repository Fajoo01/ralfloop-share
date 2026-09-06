from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Literal, Mapping

from pydantic import BaseModel, ConfigDict, Field

from .operational_runtime import BottazziOperationalRuntime
from .pec_browser_adapter import PecAuthenticatedBrowserAdapter, PecAuthenticatedCdpTransport
from .pec_runts import PecReadProvider, RuntsAuthBoundaryProvider, RuntsReadProvider
from .pec_runts_mcp import capability_descriptors
from .platform import CapabilityPermission, CapabilityRegistry


PEC_RUNTS_REFERENCE = re.compile(
    r"\bpec\b.*\b(?:pratica\s+)?runts\s+(?P<reference>[A-Za-z0-9_.:/-]{3,240})\b",
    re.IGNORECASE,
)


class PecRuntsDecisionArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")
    runts_reference: str = Field(pattern=r"^[A-Za-z0-9_.:@/-]+$", min_length=1, max_length=240)
    limit: int = Field(default=100, ge=1, le=100)


class PecRuntsToolDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["tool"]
    response: None = None
    tool_id: Literal["pec_find_by_runts_reference"]
    arguments: PecRuntsDecisionArguments


def decision_for_telegram(text: str) -> PecRuntsToolDecision | None:
    match = PEC_RUNTS_REFERENCE.search(" ".join(text.split()))
    if not match:
        return None
    candidates = CapabilityRegistry(capability_descriptors()).retrieve(
        text, domains=("pec_runts",), allowed_permissions=(CapabilityPermission.READ,), limit=3,
    )
    compatible = [row for row in candidates if "runts_reference" in row.input_schema.get("required", ())]
    if len(compatible) != 1:
        raise ValueError("pec_runts_capability_unresolved")
    return PecRuntsToolDecision(
        action="tool", tool_id=compatible[0].capability_id,
        arguments=PecRuntsDecisionArguments(runts_reference=match.group("reference")),
    )


def execute_telegram_read(
    text: str,
    *,
    memory_path: str | Path,
    pec_provider: PecReadProvider | None = None,
    runts_provider: RuntsReadProvider | None = None,
) -> dict[str, Any]:
    decision = decision_for_telegram(text)
    if decision is None:
        raise ValueError("pec_runts_telegram_not_applicable")
    transport = None
    if pec_provider is None:
        transport = PecAuthenticatedCdpTransport()
        pec_provider = PecAuthenticatedBrowserAdapter(transport)
    try:
        with BottazziOperationalRuntime(
            memory_path, pec_provider=pec_provider,
            runts_provider=runts_provider or RuntsAuthBoundaryProvider(),
        ) as runtime:
            result = runtime.invoke_pec_runts(text, decision.arguments.model_dump())
        payload = dict(result.get("structuredContent") or {})
        messages = list(payload.get("messages") or ())
        if result.get("isError"):
            answer = f"Ricerca PEC non completata: {payload.get('status', 'SOURCE_UNAVAILABLE')}."
        elif messages:
            answer = f"Trovate {len(messages)} comunicazioni PEC con riferimento RUNTS esatto {decision.arguments.runts_reference}."
        else:
            answer = f"Nessuna comunicazione PEC con riferimento RUNTS esatto {decision.arguments.runts_reference}."
        return {
            "ok": not bool(result.get("isError")),
            "interaction_mode": "unified_assistant",
            "capability": decision.tool_id,
            "tools_executed": True,
            "response": answer,
            "final_answer": answer,
            "approval_required": False,
            "metadata": {
                "tool_decision": "tool", "tool_decision_valid": True,
                "selected_capability": result.get("selectedCapability"),
                "mcp_invoked": True, "read_result": payload,
                "writes": payload.get("writes", 0),
            },
            "artifacts": [{"artifact_type": "pec_runts_read", "status": "completed" if not result.get("isError") else payload.get("status"), "count": len(messages), "writes": 0}],
            "audit_summary": [f"pec_runts::{decision.tool_id}::read_only"],
        }
    finally:
        if transport is not None:
            transport.close()


def is_pec_runts_telegram(text: str) -> bool:
    return decision_for_telegram(text) is not None


__all__ = ["PecRuntsToolDecision", "decision_for_telegram", "execute_telegram_read", "is_pec_runts_telegram"]
