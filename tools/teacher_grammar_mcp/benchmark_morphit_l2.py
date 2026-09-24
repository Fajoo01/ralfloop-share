#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import sqlite3
from pathlib import Path

TOKEN_RE = re.compile(r"[^\W\d_][\w'’-]{0,63}", re.UNICODE)
AUX = {"essere", "avere"}
NONFINITE = {"infinito", "gerundio", "participio"}


def analyses(db: sqlite3.Connection, token: str) -> list[dict]:
    rows = db.execute(
        "SELECT l.id,l.token,l.categoria,l.source FROM lexemes l WHERE lower(l.normalized)=lower(?) AND l.status='approved' ORDER BY l.id LIMIT 8",
        (token,),
    ).fetchall()
    out = []
    for lexeme_id, surface, category, source in rows:
        feats = dict(db.execute("SELECT key,value FROM features WHERE lexeme_id=?", (lexeme_id,)))
        out.append({"token": surface, "category": category, "features": feats, "source": source})
    return out


def participles(db: sqlite3.Connection, lemma: str) -> list[str]:
    rows = db.execute(
        """
        SELECT DISTINCT l.token
        FROM lexemes l
        JOIN features f ON f.lexeme_id=l.id AND f.key='lemma' AND lower(f.value)=lower(?)
        WHERE l.status='approved' AND l.categoria='verbo'
          AND EXISTS(SELECT 1 FROM features x WHERE x.lexeme_id=l.id AND x.key='modo' AND x.value='participio')
          AND EXISTS(SELECT 1 FROM features x WHERE x.lexeme_id=l.id AND x.key='tempo' AND x.value='passato')
        ORDER BY l.token LIMIT 32
        """,
        (lemma,),
    ).fetchall()
    return [row[0] for row in rows]


def naive_aux_inf(db: sqlite3.Connection, text: str) -> tuple[bool, str | None, str | None, str | None]:
    tokens = [t.casefold().replace("’", "'") for t in TOKEN_RE.findall(text)]
    cache = {token: analyses(db, token) for token in dict.fromkeys(tokens)}
    for first, second in zip(tokens, tokens[1:]):
        left = cache[first]
        right = cache[second]
        finite_aux = any(
            row["category"] == "verbo"
            and row["features"].get("lemma", "").casefold() in AUX
            and row["features"].get("modo", "").casefold() not in NONFINITE
            for row in left
        )
        if not finite_aux:
            continue
        infinitives = [
            row["features"].get("lemma", "").casefold()
            for row in right
            if row["category"] == "verbo" and row["features"].get("modo", "").casefold() == "infinito"
        ]
        if infinitives:
            return True, first, second, infinitives[0]
    return False, None, None, None


def has_analysis(rows: list[dict], **features: str) -> bool:
    return any(all(row["features"].get(k) == v for k, v in features.items()) for row in rows)


def main() -> int:
    parser = argparse.ArgumentParser(description="Diagnostic benchmark: what Morph-it! can and cannot safely support for Italian L2.")
    parser.add_argument("--db", required=True, type=Path)
    parser.add_argument("--cases", required=True, type=Path)
    args = parser.parse_args()

    cases = [json.loads(line) for line in args.cases.read_text(encoding="utf-8").splitlines() if line.strip()]
    db = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    results = []
    tp = fp = tn = fn = 0
    candidate_hits = preferred_hits = preferred_total = 0

    for case in cases:
        detected, auxiliary, infinitive, lemma = naive_aux_inf(db, case["text"])
        expected = bool(case["should_flag"])
        if detected and expected: tp += 1
        elif detected and not expected: fp += 1
        elif not detected and expected: fn += 1
        else: tn += 1
        forms = participles(db, lemma) if lemma else []
        if expected and forms:
            candidate_hits += 1
        preferred = case.get("preferred") or []
        if expected and preferred:
            preferred_total += 1
            if any(value.casefold() in {f.casefold() for f in forms} for value in preferred):
                preferred_hits += 1
        results.append({
            "id": case["id"], "expected_flag": expected, "detected": detected,
            "auxiliary": auxiliary, "infinitive": infinitive, "lemma": lemma,
            "candidate_participles": forms[:8], "preferred": preferred,
            "preferred_hit": bool(preferred and any(value.casefold() in {f.casefold() for f in forms} for value in preferred)),
        })

    positive = tp + fn
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / positive if positive else 0.0
    sono = analyses(db, "sono")
    abbiamo = analyses(db, "abbiamo")
    summary = {
        "cases": len(cases), "tp": tp, "fp": fp, "tn": tn, "fn": fn,
        "precision": round(precision, 3), "recall": round(recall, 3),
        "positive_candidate_coverage": round(candidate_hits / positive, 3) if positive else 0.0,
        "preferred_form_coverage": round(preferred_hits / preferred_total, 3) if preferred_total else 0.0,
        "known_limitations": {
            "sono_has_1sg_indicative_present": has_analysis(sono, lemma="essere", modo="indicativo", tempo="presente", persona="prima", numero="singolare"),
            "sono_has_3pl_indicative_present": has_analysis(sono, lemma="essere", modo="indicativo", tempo="presente", persona="terza", numero="plurale"),
            "abbiamo_has_1pl_indicative_present": has_analysis(abbiamo, lemma="avere", modo="indicativo", tempo="presente", persona="prima", numero="plurale"),
        },
        "conclusion": "morphology_candidate_source_not_contextual_truth",
    }
    print(json.dumps({"summary": summary, "results": results}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
