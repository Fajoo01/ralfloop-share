from __future__ import annotations

import json

def validate_grammar_output(final_answer: str) -> dict:
    try:
        arr = json.loads(final_answer or "")
    except Exception:
        return {
            "ok": False,
            "reason": "invalid_json",
            "details": [{"error": "json_decode_failed"}],
        }

    if not isinstance(arr, list) or not arr:
        return {
            "ok": False,
            "reason": "empty_or_non_list",
            "details": [{"error": "not_a_nonempty_list"}],
        }

    by_token = {}
    for item in arr:
        if not isinstance(item, dict):
            continue
        tok = str(item.get("token") or "").strip()
        if tok:
            by_token[tok] = item

    if not by_token:
        return {
            "ok": False,
            "reason": "missing_tokens",
            "details": [{"error": "no_token_entries"}],
        }

    checks = [
        ("La", ["genere", "numero"]),
        ("lo", ["genere", "numero"]),
        ("un", ["genere", "numero"]),
        ("bambina", ["genere", "numero"]),
        ("libro", ["genere", "numero"]),
        ("legge", ["lemma"]),
        ("Devi", ["lemma", "modo", "tempo", "persona", "numero"]),
        ("ordinare", ["lemma"]),
    ]

    found_relevant = False
    issues = []

    for tok, fields in checks:
        item = by_token.get(tok)
        if not item:
            continue
        found_relevant = True
        missing = []
        for f in fields:
            if not str(item.get(f) or "").strip():
                missing.append(f)
        if missing:
            issues.append({
                "token": tok,
                "missing": missing,
                "current_item": item,
            })

    if not found_relevant:
        return {
            "ok": False,
            "reason": "no_relevant_tokens_found",
            "details": [{"error": "validator_could_not_apply"}],
        }

    if issues:
        return {
            "ok": False,
            "reason": "missing_required_fields",
            "details": issues,
            "suggested_target": "grammar",
            "suggested_file": "openshell_backend/skill_grammar_rag.py",
            "required_changes": [
                "add enrichment for lemma/genere/numero/modo/tempo/persona where inferable",
                "improve local fallback for articles verbs and common nouns",
                "preserve current json output structure",
                "pass tests/grammar/run_grammar_tests.py",
            ],
        }

    return {
        "ok": True,
        "reason": "sufficient",
        "details": [],
        "suggested_target": "grammar",
        "suggested_file": "openshell_backend/skill_grammar_rag.py",
    }

def validate_skill_output(skill_name: str, final_answer: str) -> dict:
    name = str(skill_name or "").strip().lower()
    if name == "grammar":
        return validate_grammar_output(final_answer)
    return {
        "ok": True,
        "reason": "no_validator_for_skill",
        "details": [],
    }

def is_grammar_output_sufficient(final_answer: str) -> bool:
    return bool(validate_grammar_output(final_answer).get("ok"))

def is_skill_output_sufficient(skill_name: str, final_answer: str) -> bool:
    return bool(validate_skill_output(skill_name, final_answer).get("ok"))
