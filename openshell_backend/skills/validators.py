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

    valid_items = []
    missing = []

    for idx, item in enumerate(arr):
        if not isinstance(item, dict):
            continue

        token = str(item.get("token") or "").strip()
        categoria = str(item.get("categoria") or "").strip()

        if token and categoria:
            valid_items.append({
                "index": idx,
                "token": token,
                "categoria": categoria,
            })
        else:
            missing.append({
                "index": idx,
                "token": token,
                "categoria": categoria,
                "missing": [
                    name for name, value in (
                        ("token", token),
                        ("categoria", categoria),
                    ) if not value
                ],
                "current_item": item,
            })

    if not valid_items:
        return {
            "ok": False,
            "reason": "no_valid_grammar_entries",
            "details": missing or [{"error": "no_items_with_token_and_categoria"}],
            "suggested_target": "grammar",
            "suggested_file": "openshell_backend/skill_grammar_rag.py",
        }

    return {
        "ok": True,
        "reason": "sufficient",
        "details": [],
        "summary": {
            "total_items": len(arr),
            "valid_items": len(valid_items),
        },
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
