#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ralfloop_agent.teacher.grammar_client import _surface_tokens, contextual_l2_checks


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


def participles(db: sqlite3.Connection, lemma: str) -> list[dict]:
    rows = db.execute(
        """
        SELECT DISTINCT l.id,l.token,l.categoria,l.source
        FROM lexemes l
        JOIN features f ON f.lexeme_id=l.id AND f.key='lemma' AND lower(f.value)=lower(?)
        WHERE l.status='approved' AND l.categoria='verbo'
          AND EXISTS(SELECT 1 FROM features x WHERE x.lexeme_id=l.id AND x.key='modo' AND x.value='participio')
          AND EXISTS(SELECT 1 FROM features x WHERE x.lexeme_id=l.id AND x.key='tempo' AND x.value='passato')
        ORDER BY l.token LIMIT 32
        """,
        (lemma,),
    ).fetchall()
    out = []
    for lexeme_id, token, category, source in rows:
        feats = dict(db.execute("SELECT key,value FROM features WHERE lexeme_id=?", (lexeme_id,)))
        out.append({"token": token, "category": category, "features": feats, "source": source})
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description="Benchmark Morph-it-backed contextual supervision for Italian L2.")
    parser.add_argument("--db", required=True, type=Path)
    parser.add_argument("--cases", required=True, type=Path)
    args = parser.parse_args()

    cases = [json.loads(line) for line in args.cases.read_text(encoding="utf-8").splitlines() if line.strip()]
    db = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    results = []
    tp = fp = tn = fn = 0
    candidate_hits = 0
    positives = 0
    ambiguous_total = 0
    suppressed_total = 0

    for case in cases:
        tokens = _surface_tokens(case["text"])
        by_token = {token: analyses(db, token) for token in dict.fromkeys(tokens)}
        infinitive_lemmas = set()
        for rows in by_token.values():
            for row in rows:
                feats = row.get("features") or {}
                if row.get("category") == "verbo" and feats.get("modo") == "infinito" and feats.get("lemma"):
                    infinitive_lemmas.add(str(feats["lemma"]).casefold())
        forms = {lemma: participles(db, lemma) for lemma in infinitive_lemmas}
        contextual = contextual_l2_checks(case["text"], analyses_by_token=by_token, participle_forms=forms)
        detected = bool(contextual["issues"])
        expected = bool(case["should_flag"])
        if detected and expected:
            tp += 1
        elif detected and not expected:
            fp += 1
        elif not detected and expected:
            fn += 1
        else:
            tn += 1
        if expected:
            positives += 1
            if any(issue.get("candidate_participles") for issue in contextual["issues"]):
                candidate_hits += 1
        ambiguous_total += len(contextual["ambiguous"])
        suppressed_total += len(contextual["suppressed"])
        results.append({
            "id": case["id"],
            "expected_flag": expected,
            "detected": detected,
            "issues": contextual["issues"],
            "ambiguous": contextual["ambiguous"],
            "suppressed": contextual["suppressed"],
        })

    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    summary = {
        "cases": len(cases),
        "tp": tp,
        "fp": fp,
        "tn": tn,
        "fn": fn,
        "precision": round(precision, 3),
        "recall": round(recall, 3),
        "positive_candidate_coverage": round(candidate_hits / positives, 3) if positives else 0.0,
        "ambiguous_not_autocorrected": ambiguous_total,
        "metalinguistic_or_quoted_suppressed": suppressed_total,
        "goal": "precision>=0.95_recall>=0.95_on_v1_controls",
        "goal_reached": precision >= 0.95 and recall >= 0.95,
        "conclusion": "morphit_backed_contextual_supervision_not_lexicon_only",
    }
    print(json.dumps({"summary": summary, "results": results}, ensure_ascii=False, indent=2))
    return 0 if summary["goal_reached"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
