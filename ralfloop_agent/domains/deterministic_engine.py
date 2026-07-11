from __future__ import annotations

import re
from typing import Any


class DeterministicEngine:
    def evaluate(self, domain: dict[str, Any], request: str | dict[str, Any]) -> dict[str, Any]:
        text = request.get("goal", "") if isinstance(request, dict) else str(request)
        manifest = domain["manifest"]
        if manifest.get("state") != "active":
            return _empty("domain_not_active")
        did = manifest["domain_id"]
        if did == "arithmetic_basic":
            return self._arithmetic(domain, text)
        if did == "incident_triage":
            return self._incident(domain, text)
        return _empty("no_engine_for_domain")

    def classify(self, domain: dict[str, Any], goal: str) -> str:
        if domain["manifest"].get("state") != "active":
            return "out_of_scope"
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


def _empty(reason: str) -> dict[str, Any]:
    return {"matched_rules": [], "decision_tables_used": [], "facts": {}, "derived_values": {}, "result": None, "unresolved_questions": [reason], "conflicts": [], "confidence": 0.0, "complete": False, "blocking_errors": [], "evidence_refs": []}
