from __future__ import annotations

from dataclasses import dataclass
import os
from typing import Any

from .jury_router import DomainJuryRouter, RECURSIVE_REASON_CODES


FEATURE_ENV = {
    "qualitative_judgment": "RALF_RECURSIVE_QUALITATIVE_JUDGMENT",
    "strategic_assessment": "RALF_RECURSIVE_STRATEGIC_ASSESSMENT",
    "recommendation_required": "RALF_RECURSIVE_QUALITATIVE_JUDGMENT",
    "conflicting_sources": "RALF_RECURSIVE_CONFLICTING_SOURCES",
    "incomplete_rules": "RALF_RECURSIVE_QUALITATIVE_JUDGMENT",
    "evidence_synthesis": "RALF_RECURSIVE_EVIDENCE_SYNTHESIS",
    "domain_validation": "RALF_RECURSIVE_DOMAIN_VALIDATION",
    "domain_creation_review": "RALF_RECURSIVE_DOMAIN_VALIDATION",
}


@dataclass(frozen=True)
class RecursiveRoutingFlags:
    enabled_reason_codes: frozenset[str] = frozenset()
    hybrid_enabled: bool = False

    @classmethod
    def from_env(cls) -> "RecursiveRoutingFlags":
        enabled = frozenset(reason for reason, name in FEATURE_ENV.items() if os.getenv(name, "0") == "1")
        return cls(enabled, os.getenv("RALF_RECURSIVE_HYBRID_ENABLED", "0") == "1")


class DomainReasoningRouter:
    def __init__(self, flags: RecursiveRoutingFlags | None = None) -> None:
        self.flags = flags or RecursiveRoutingFlags.from_env()

    def select(
        self,
        *,
        domain_resolution: dict[str, Any],
        classification: str,
        deterministic_result: dict[str, Any] | None = None,
        requested_reason_codes: list[str] | None = None,
        explicit_recursive: bool = False,
        prefer_hybrid: bool = False,
        domain_creation_review: bool = False,
    ) -> dict[str, Any]:
        jury = DomainJuryRouter().should_use_jury(
            domain_resolution=domain_resolution,
            classification=classification,
            deterministic_result=deterministic_result,
            requested_reason_codes=requested_reason_codes,
            domain_creation_review=domain_creation_review,
        )
        reasons = list(jury["reason_codes"])
        status = str(domain_resolution.get("status") or "missing")
        if status != "resolved" and not domain_creation_review:
            return _route("domain_creation_required", reasons, False, "domain_not_resolved")
        if not jury["use_jury"]:
            selected = "deterministic_engine" if reasons[0] in {"deterministic_complete", "lookup_complete", "deterministic", "arithmetic_basic", "strict_transformation", "code_fix_deterministic"} else "single_qwen_7b_with_domain"
            return _route(selected, reasons, False, "recursive_reason_code_absent")
        eligible = [reason for reason in reasons if reason in RECURSIVE_REASON_CODES]
        enabled = explicit_recursive or any(reason in self.flags.enabled_reason_codes for reason in eligible)
        if not enabled:
            return _route("single_qwen_7b_with_domain", reasons, False, "recursive_feature_disabled")
        if prefer_hybrid and self.flags.hybrid_enabled:
            return _route("recursive_mas_text_hybrid", reasons, True, "eligible_reason_and_hybrid_enabled")
        return _route("recursive_mas_native", reasons, True, "eligible_reason")


def _route(selected: str, reasons: list[str], recursive: bool, why: str) -> dict[str, Any]:
    return {
        "selected_backend": selected,
        "reason_codes": reasons,
        "use_recursive": recursive,
        "why_recursive": why if recursive else None,
        "why_not_recursive": None if recursive else why,
    }
