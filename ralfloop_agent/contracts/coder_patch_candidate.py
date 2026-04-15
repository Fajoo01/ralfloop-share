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



def build_coder_handoff_prompt(autofix_candidate: dict[str, Any]) -> str | None:
    patch = build_coder_patch_candidate(autofix_candidate)
    if not isinstance(patch, dict):
        return None

    instructions = patch.get("instructions") or []
    example_cases = patch.get("example_cases") or []

    lines = [
        "Produce a minimal patch for the target symbol.",
        f"Target file: {patch.get('target_file')}",
        f"Target symbol: {patch.get('target_symbol')}",
        f"Issue kind: {patch.get('issue_kind')}",
        f"Why: {patch.get('why')}",
        "",
        "Required changes:",
    ]
    for item in instructions:
        lines.append(f"- {item}")

    if example_cases:
        lines.append("")
        lines.append("Example cases to preserve/fix:")
        for item in example_cases:
            lines.append(f"- {item}")

    lines.extend([
        "",
        "Return only the patch-ready code change for the target file.",
        "Do not refactor unrelated code.",
        "Keep existing tests green.",
    ])
    return "\n".join(lines)


