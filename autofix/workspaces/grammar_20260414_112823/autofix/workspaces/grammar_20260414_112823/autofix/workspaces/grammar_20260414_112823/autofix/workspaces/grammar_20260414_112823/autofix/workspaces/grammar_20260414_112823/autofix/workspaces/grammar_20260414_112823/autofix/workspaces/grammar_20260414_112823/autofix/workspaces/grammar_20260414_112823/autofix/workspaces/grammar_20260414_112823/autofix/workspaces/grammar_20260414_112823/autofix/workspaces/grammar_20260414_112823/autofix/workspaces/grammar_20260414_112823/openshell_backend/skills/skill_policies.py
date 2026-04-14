from __future__ import annotations

SKILL_POLICIES = {
    "grammar": {
        "quality_policy": "always",
        "quality_validator": "grammar",
    },
    # esempi futuri
    "ner": {
        "quality_policy": "auto",
        "quality_validator": "ner",
    },
    "math_fast": {
        "quality_policy": "none",
        "quality_validator": None,
    },
}

def get_skill_policy(skill_name: str) -> dict:
    return dict(SKILL_POLICIES.get(str(skill_name or "").strip().lower(), {
        "quality_policy": "none",
        "quality_validator": None,
    }))
