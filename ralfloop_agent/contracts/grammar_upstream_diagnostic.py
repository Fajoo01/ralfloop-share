from __future__ import annotations

from typing import Any


def build_grammar_upstream_diagnostic(user_goal: str, analyzed: dict[str, Any]) -> dict[str, Any] | None:
    if not isinstance(analyzed, dict):
        return None

    raw_issues = analyzed.get("raw_issues")
    if not isinstance(raw_issues, list) or not raw_issues:
        return None

    return {
        "kind": "grammar_analysis_suspicions",
        "source": "openshell_backend.skill_grammar_rag",
        "user_goal": user_goal,
        "raw_issues": raw_issues,
        "raw_items": analyzed.get("raw_items", []),
        "final_items": analyzed.get("final_items", analyzed.get("items", [])),
        "final_issues": analyzed.get("final_issues", analyzed.get("issues", [])),
        "suggested_action": "inspect_and_patch_grammar_tokenizer_or_fallback",
    }
