from __future__ import annotations

from typing import Callable

from openshell_backend.skill_grammar_rag import maybe_answer_grammar_request as grammar__fn

SkillFn = Callable[[str], str | None]

DIRECT_SKILLS: list[tuple[str, SkillFn]] = [
    ("grammar", grammar__fn),
]

def try_direct_skill(user_goal: str) -> tuple[str, str] | None:
    text = (user_goal or "").strip()
    if not text:
        return None

    for name, fn in DIRECT_SKILLS:
        try:
            out = fn(text)
        except Exception:
            out = None
        if out:
            return name, out

    return None
