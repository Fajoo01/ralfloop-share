from __future__ import annotations

from typing import Any


def find_grammar_suspicions(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []

    for i, item in enumerate(items):
        tok = str(item.get("token") or "")
        cat = str(item.get("categoria") or "")

        if "'" in tok and cat == "nome_comune":
            low = tok.lower()
            if low.startswith(("dall'", "all'", "nell'", "sull'", "coll'", "dell'", "l'", "un'")):
                issues.append({
                    "kind": "suspicious_apostrophe_token",
                    "index": i,
                    "token": tok,
                    "categoria": cat,
                    "reason": "token con apostrofo classificato come nome_comune",
                    "suggested_fix": "split_apostrophe_token_before_classification",
                })

    return issues
