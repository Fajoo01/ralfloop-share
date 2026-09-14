#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
from pathlib import Path
import shutil
import sqlite3
import unicodedata
from typing import Iterable


BULK_PROPOSAL = "bulk_morphit_048_v2_latin1"
INDEFINITE_ARTICLES = {"un", "uno", "una", "un'"}
VERB_BASES = {"VER", "AUX", "MOD", "CAU", "ASP"}
PUNCT_BASES = {"PON", "SENT", "SYM"}
CLITIC_BASES = {"CE", "CI", "NE", "SI"}


def canonical_key(value: str) -> str:
    return unicodedata.normalize("NFC", str(value or "")).casefold().replace("’", "'")


def _parts(tag: str) -> tuple[str, list[str]]:
    base, _, detail = tag.partition(":")
    return base, detail.split("+") if detail else []


def categories(token: str, tag: str) -> tuple[str, ...]:
    base, _ = _parts(tag)
    if base.startswith("NOUN"): return ("nome_comune",)
    if base == "NPR": return ("nome_proprio",)
    if base == "ADJ": return ("aggettivo_qualificativo",)
    if base.startswith("DET-POSS"): return ("aggettivo_possessivo",)
    if base.startswith("DET-INDEF"): return ("aggettivo_indefinito",)
    if base.startswith("DET-DEMO"): return ("aggettivo_dimostrativo",)
    if base.startswith("DET-WH"): return ("aggettivo_interrogativo",)
    if base.startswith("DET-NUM"): return ("numerale",)
    if base.startswith("ARTPRE"): return ("preposizione_articolata",)
    if base.startswith("ART"):
        return (("articolo_indeterminativo",) if canonical_key(token) in INDEFINITE_ARTICLES else ("articolo_determinativo",))
    if base == "ADV": return ("avverbio",)
    if base == "PRE": return ("preposizione",)
    if base == "CON": return ("congiunzione",)
    if base == "WH": return ("avverbio", "congiunzione")
    if base == "WH-CHE": return ("pronome", "congiunzione")
    if base.startswith("PRO") or base in CLITIC_BASES: return ("pronome",)
    if base in VERB_BASES: return ("verbo",)
    if base in PUNCT_BASES: return ("segno_punteggiatura",)
    if base == "INT": return ("interiezione",)
    if base == "TALE": return ("aggettivo_qualificativo", "pronome")
    if base == "ABL": return ("abbreviazione",)
    return ("non_classificato",)


def features(lemma: str, tag: str) -> dict[str, str]:
    base, parts = _parts(tag)
    out = {"lemma": lemma, "morphit_tag": tag}
    if "-M" in base or "m" in parts: out["genere"] = "maschile"
    if "-F" in base or "f" in parts: out["genere"] = "femminile"
    if "s" in parts or (base.startswith("NOUN") and tag.endswith(":s")): out["numero"] = "singolare"
    if "p" in parts or (base.startswith("NOUN") and tag.endswith(":p")): out["numero"] = "plurale"
    if base in VERB_BASES:
        modes = {"ind":"indicativo","sub":"congiuntivo","cond":"condizionale","imp":"imperativo","inf":"infinito","ger":"gerundio","part":"participio"}
        times = {"pres":"presente","past":"passato","fut":"futuro","impf":"imperfetto"}
        for key, value in modes.items():
            if key in parts: out["modo"] = value
        for key, value in times.items():
            if key in parts: out["tempo"] = value
        for n, value in {"1":"prima","2":"seconda","3":"terza"}.items():
            if n in parts: out["persona"] = value
    if base.startswith("DET-"):
        out["determinante"] = base.removeprefix("DET-").casefold()
    return out


def source_rows(path: Path) -> Iterable[tuple[str, str, str]]:
    with path.open("r", encoding="latin-1", newline="") as stream:
        for raw in stream:
            pieces = raw.rstrip("\r\n").split("\t")
            if len(pieces) != 3:
                continue
            token, lemma, tag = (piece.strip() for piece in pieces)
            if token and lemma and tag:
                yield token, lemma, tag


def clear_old_bulk(db: sqlite3.Connection) -> None:
    ids = [row[0] for row in db.execute("SELECT id FROM lexemes WHERE source='bulk_morphit'")]
    for start in range(0, len(ids), 5000):
        chunk = ids[start:start + 5000]
        marks = ",".join("?" for _ in chunk)
        db.execute(f"DELETE FROM features WHERE lexeme_id IN ({marks})", chunk)
        db.execute(f"DELETE FROM provenance WHERE lexeme_id IN ({marks})", chunk)
        db.execute(f"DELETE FROM lexemes WHERE id IN ({marks})", chunk)


def ensure_indexes(db: sqlite3.Connection) -> None:
    db.execute("CREATE INDEX IF NOT EXISTS idx_lexemes_status_norm ON lexemes(status,normalized)")
    db.execute("CREATE INDEX IF NOT EXISTS idx_lexemes_norm_cat ON lexemes(normalized,categoria)")
    db.execute("CREATE INDEX IF NOT EXISTS idx_features_lexeme ON features(lexeme_id)")


def import_morphit(db: sqlite3.Connection, source: Path) -> int:
    source_hash = hashlib.sha256(source.read_bytes()).hexdigest()
    now = "2026-09-14T00:00:00Z"
    count = 0
    for token, lemma, tag in source_rows(source):
        feats = features(lemma, tag)
        feats["source_hash"] = source_hash
        feats["source_name"] = "Morph-it! 0.4.8"
        feats["source_encoding"] = "latin-1"
        for category in categories(token, tag):
            normalized = canonical_key(token)
            db.execute(
                "INSERT OR IGNORE INTO lexemes "
                "(token,normalized,categoria,status,source,proposal_id,approved_by,approved_at,created_at,updated_at) "
                "VALUES(?,?,?,'approved','bulk_morphit',?,'bulk_import_v2',?,?,?)",
                (token, normalized, category, BULK_PROPOSAL, now, now, now),
            )
            row = db.execute(
                "SELECT id,source,proposal_id FROM lexemes WHERE normalized=? AND categoria=?",
                (normalized, category),
            ).fetchone()
            if row is None or (row[1] != "bulk_morphit" and row[2] != BULK_PROPOSAL):
                continue
            lexeme_id = int(row[0])
            for key, value in feats.items():
                if value:
                    db.execute(
                        "INSERT OR REPLACE INTO features(lexeme_id,key,value) VALUES(?,?,?)",
                        (lexeme_id, key, value),
                    )
            count += 1
    db.execute("CREATE TABLE IF NOT EXISTS grammar_build_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL)")
    for key, value in {
        "morphit_sha256": source_hash,
        "morphit_encoding": "latin-1",
        "morphit_version": "0.4.8",
        "builder": "ralf-teacher-grammar-v2",
    }.items():
        db.execute("INSERT OR REPLACE INTO grammar_build_meta(key,value) VALUES(?,?)", (key, value))
    return count


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--legacy-db", required=True, type=Path)
    parser.add_argument("--morphit", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    if args.output.exists():
        raise SystemExit("output_exists")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(args.legacy_db, args.output)
    db = sqlite3.connect(args.output)
    try:
        db.execute("PRAGMA foreign_keys=OFF")
        db.execute("PRAGMA journal_mode=MEMORY")
        db.execute("PRAGMA synchronous=OFF")
        db.execute("PRAGMA temp_store=MEMORY")
        db.execute("BEGIN IMMEDIATE")
        clear_old_bulk(db)
        imported = import_morphit(db, args.morphit)
        ensure_indexes(db)
        db.commit()
        db.execute("ANALYZE")
        db.commit()
        integrity = db.execute("PRAGMA integrity_check").fetchone()[0]
        counts = {
            table: db.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            for table in ("lexemes", "features", "valency_frames", "valency_roles", "tpas_verbs", "tpas_patterns")
        }
    except Exception:
        db.rollback()
        db.close()
        args.output.unlink(missing_ok=True)
        raise
    db.close()
    if integrity != "ok":
        args.output.unlink(missing_ok=True)
        raise SystemExit(f"integrity_failed:{integrity}")
    print({"imported": imported, "counts": counts, "output": str(args.output)})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
