from __future__ import annotations

import os
import re
from typing import Any

from .models import DomainResolution
from .registry import DomainRegistry, fixture_domain

EXACT = float(os.getenv("DOMAIN_RESOLVE_EXACT", "1.0"))
HIGH = float(os.getenv("DOMAIN_RESOLVE_HIGH", "0.85"))
AMBIG = float(os.getenv("DOMAIN_RESOLVE_AMBIGUOUS", "0.60"))


class DomainResolver:
    def __init__(self, registry: DomainRegistry | None = None) -> None:
        self.registry = registry or DomainRegistry()

    def resolve(self, user_goal: str, context: dict[str, Any] | None = None) -> DomainResolution:
        context = context or {}
        explicit = context.get("domain") or context.get("domain_id") or _extract_explicit(user_goal)
        if explicit:
            domain = self.registry.find_active(str(explicit))
            if domain:
                m = domain["manifest"]
                return DomainResolution("resolved", m["domain_id"], m["version"], EXACT, [{"domain_id": m["domain_id"], "confidence": EXACT}], ["explicit_domain"])
            return DomainResolution("missing", str(explicit), None, 0.0, [], ["explicit_domain_missing"])
        candidates = self._candidates(user_goal)
        if not candidates:
            return DomainResolution("missing", None, None, 0.0, [], ["below_ambiguous_threshold"])
        candidates.sort(key=lambda item: item["confidence"], reverse=True)
        top = candidates[0]
        plausible = [item for item in candidates if item["confidence"] >= AMBIG]
        if top["confidence"] < AMBIG:
            return DomainResolution("missing", None, None, top["confidence"], candidates, ["below_ambiguous_threshold"])
        if len(plausible) > 1 and plausible[1]["confidence"] >= AMBIG and abs(top["confidence"] - plausible[1]["confidence"]) < 0.20:
            return DomainResolution("ambiguous", None, None, top["confidence"], plausible, ["multiple_active_domains"], True)
        return DomainResolution("resolved", top["domain_id"], top["version"], top["confidence"], candidates, top["reason_codes"])

    def _candidates(self, goal: str) -> list[dict[str, Any]]:
        active = [self.registry.find_active("arithmetic_basic"), self.registry.find_active("incident_triage")]
        for row in self.registry.list_domains("active"):
            if row.get("domain_id") in {"arithmetic_basic", "incident_triage"}:
                continue
            loaded = self.registry.get_domain(str(row.get("domain_id")), str(row.get("version")))
            if loaded:
                active.append(loaded)
        seen = set()
        out = []
        terms = set(re.findall(r"[\w+]+", goal.lower()))
        for domain in active:
            if not domain:
                continue
            m = domain["manifest"]
            did = m["domain_id"]
            if did in seen:
                continue
            seen.add(did)
            aliases = set(x.lower() for x in m.get("aliases", []))
            scope_words = set()
            for item in m.get("scope", []):
                scope_words.update(re.findall(r"[\w+]+", str(item).lower()))
            score = 0.0
            reasons = []
            if did.lower() in goal.lower() or any(alias in goal.lower() for alias in aliases):
                score = max(score, 1.0); reasons.append("alias_exact")
            overlap = terms & scope_words
            if overlap:
                score = max(score, min(0.95, 0.55 + 0.10 * len(overlap))); reasons.append("scope_terms")
            if did == "arithmetic_basic" and re.search(r"\d+\s*\+\s*\d+|quanto fa", goal.lower()):
                score = max(score, 1.0); reasons.append("capability_integer_addition")
            if did == "incident_triage" and any(w in goal.lower() for w in ("incidente", "incident", "severity", "strategia", "prudente")):
                score = max(score, 0.9); reasons.append("capability_incident_triage")
            if score > 0:
                out.append({"domain_id": did, "version": m.get("version"), "confidence": round(score, 3), "reason_codes": reasons})
        return out


def _extract_explicit(goal: str) -> str | None:
    m = re.search(r"(?:domain|dominio)[:=]\s*([a-zA-Z0-9_.-]+)", goal)
    return m.group(1) if m else None
