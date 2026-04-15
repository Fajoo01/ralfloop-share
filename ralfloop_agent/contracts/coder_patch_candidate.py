from __future__ import annotations

from typing import Any


def build_coder_patch_candidate(autofix_candidate: dict[str, Any]) -> dict[str, Any] | None:
    if not isinstance(autofix_candidate, dict):
        return None

    diagnosis = autofix_candidate.get("diagnosis") or {}
    if not isinstance(diagnosis, dict):
        return None

    if diagnosis.get("kind") != "grammar_upstream_issue":
        return None

    upstream = autofix_candidate.get("upstream_diagnostic") or {}
    if not isinstance(upstream, dict):
        return None

    raw_issues = upstream.get("raw_issues") or []
    if not isinstance(raw_issues, list) or not raw_issues:
        return None

    first = raw_issues[0] if isinstance(raw_issues[0], dict) else {}
    if first.get("kind") != "suspicious_apostrophe_token":
        return None

    return {
        "kind": "code_patch",
        "strategy": "patch_existing_symbol",
        "target_file": diagnosis.get("target_file") or "openshell_backend/skill_grammar_rag.py",
        "target_symbol": diagnosis.get("target_symbol") or "_simple_local_grammar_fallback",
        "issue_kind": first.get("kind"),
        "why": diagnosis.get("why") or "",
        "instructions": [
            "Patch the grammar tokenizer/fallback before classification.",
            "Split apostrophe tokens like dall'Ikea into left+right parts before assigning categoria.",
            "Prefer a pre-classification token split over a post-hoc repair.",
            "Keep existing tests green and add/update grammar tests for apostrophe token splitting.",
        ],
        "example_cases": [
            "Devi ordinare lo scolapiatti dall'Ikea",
            "Vado all'Ikea",
            "L'amico arriva",
            "Il gatto è sull'albero",
        ],
    }
