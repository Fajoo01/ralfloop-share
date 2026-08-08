from __future__ import annotations

import re
from typing import Any


class DeterministicEngine:
    def evaluate(self, domain: dict[str, Any], request: str | dict[str, Any]) -> dict[str, Any]:
        text = request.get("goal", "") if isinstance(request, dict) else str(request)
        manifest = domain["manifest"]
        if manifest.get("state") != "active":
            return _empty("domain_not_active")
        if manifest.get("domain_id") == "bandi_framework":
            return _empty("meta_framework_not_operational")
        if isinstance(request, dict) and request.get("calculation"):
            from .calculation_orchestrator import CalculationOrchestrator

            result = CalculationOrchestrator().calculate(request["calculation"])
            complete = result.get("status") == "completed" and bool(result.get("deterministic")) and not result.get("jury_required")
            return {
                "matched_rules": ["calculation_orchestrator"] if complete else [],
                "decision_tables_used": [],
                "facts": {"calculation": True},
                "derived_values": result,
                "result": result,
                "unresolved_questions": result.get("jury_reason_codes", []) if not complete else [],
                "conflicts": [],
                "confidence": 1.0 if complete else 0.0,
                "complete": complete,
                "blocking_errors": [],
                "evidence_refs": result.get("sources", []),
                "calculation_result": result,
            }
        if isinstance(request, dict) and request.get("capability_id") and request.get("capability_id") in manifest.get("deterministic_capabilities", []):
            return self._canonical_capability(manifest, request)
        did = manifest["domain_id"]
        if did == "arithmetic_basic":
            return self._arithmetic(domain, text)
        if did == "incident_triage":
            return self._incident(domain, text)
        return _empty("no_engine_for_domain")

    def classify(self, domain: dict[str, Any], goal: str) -> str:
        if domain["manifest"].get("state") != "active":
            return "out_of_scope"
        if domain["manifest"]["domain_id"] == "bandi_framework":
            return "out_of_scope"
        if isinstance(goal, str) and domain["manifest"]["domain_id"] in {"abc_reasoning"}:
            return "mixed"
        lower = goal.lower()
        if domain["manifest"]["domain_id"] == "arithmetic_basic" and re.search(r"\d+\s*\+\s*\d+", lower):
            return "deterministic"
        if any(x in lower for x in ("telegram", "email", "cancello", "scrivi file")):
            return "external_action"
        if any(x in lower for x in ("strategia", "prudente", "raccomanda", "qualit")):
            return "non_deterministic"
        return "mixed"

    def _arithmetic(self, domain: dict[str, Any], text: str) -> dict[str, Any]:
        m = re.search(r"(-?\d+)\s*\+\s*(-?\d+)", text)
        if not m:
            return _empty("missing_integer_addition")
        a, b = int(m.group(1)), int(m.group(2))
        result = str(a + b)
        return {"matched_rules": ["add_integer_pair"], "decision_tables_used": [], "facts": {"a": a, "b": b}, "derived_values": {"sum": result}, "result": result, "unresolved_questions": [], "conflicts": [], "confidence": 1.0, "complete": True, "blocking_errors": [], "evidence_refs": ["local_arithmetic_policy"]}

    def _incident(self, domain: dict[str, Any], text: str) -> dict[str, Any]:
        lower = text.lower()
        matched = []
        derived = {}
        unresolved = []
        if "down" in lower and ("utenti" in lower or "users" in lower):
            matched.append("sev1_down"); derived["severity"] = "sev1"
        if any(x in lower for x in ("strategia", "prudente", "raccomanda")):
            unresolved.append("recommendation_required")
        conflicts = domain.get("conflicts", []) if unresolved else []
        complete = bool(matched) and not unresolved
        return {"matched_rules": matched, "decision_tables_used": [], "facts": {}, "derived_values": derived, "result": derived or None, "unresolved_questions": unresolved, "conflicts": conflicts, "confidence": 1.0 if matched else 0.0, "complete": complete, "blocking_errors": [], "evidence_refs": ["local_incident_policy"] if matched else []}

    def _canonical_capability(self, manifest: dict[str, Any], request: dict[str, Any]) -> dict[str, Any]:
        from .capability_registry import CanonicalCapabilityRegistry

        capability_id = str(request["capability_id"])
        inputs = dict(request.get("inputs") or {})
        result = CanonicalCapabilityRegistry.from_mapping().execute(capability_id, inputs)
        complete = bool(result.get("ok")) and result.get("status") == "completed" and not result.get("jury_required")
        return {
            "matched_rules": [f"canonical_capability:{capability_id}"] if complete else [],
            "decision_tables_used": [],
            "facts": {"capability_id": capability_id},
            "derived_values": result.get("result", {}),
            "result": result.get("result"),
            "unresolved_questions": result.get("unresolved_questions", []),
            "conflicts": [],
            "confidence": 1.0 if complete else 0.0,
            "complete": complete,
            "blocking_errors": [] if result.get("status") != "error" else [result.get("error_type") or "capability_error"],
            "evidence_refs": result.get("evidence_refs", []),
            "capability_result": result,
            "domain_id": manifest.get("domain_id"),
        }


def _empty(reason: str) -> dict[str, Any]:
    return {"matched_rules": [], "decision_tables_used": [], "facts": {}, "derived_values": {}, "result": None, "unresolved_questions": [reason], "conflicts": [], "confidence": 0.0, "complete": False, "blocking_errors": [], "evidence_refs": []}
