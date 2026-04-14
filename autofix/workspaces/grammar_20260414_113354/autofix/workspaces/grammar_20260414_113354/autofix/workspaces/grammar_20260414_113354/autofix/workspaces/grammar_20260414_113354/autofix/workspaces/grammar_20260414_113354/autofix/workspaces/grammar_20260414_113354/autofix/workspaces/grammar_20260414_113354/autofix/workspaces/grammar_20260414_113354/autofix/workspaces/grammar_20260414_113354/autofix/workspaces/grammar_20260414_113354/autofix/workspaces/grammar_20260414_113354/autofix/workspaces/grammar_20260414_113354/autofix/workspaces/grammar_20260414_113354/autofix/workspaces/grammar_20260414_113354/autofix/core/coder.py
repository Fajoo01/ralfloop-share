from __future__ import annotations
import hashlib, shutil
from pathlib import Path
from typing import Any

BASE = Path("/home/sibilla-cumana/ralfloop_agent_scaffold")

GRAMMAR_HELPERS = r'''
def _infer_item_fields(item: dict[str, Any]) -> dict[str, Any]:
    tok = str(item.get("token") or "").strip()
    low = tok.lower()
    cat = str(item.get("categoria") or "").strip()
    out: dict[str, Any] = {}

    article_features = {
        "la": {"genere": "femminile", "numero": "singolare"},
        "lo": {"genere": "maschile", "numero": "singolare"},
        "il": {"genere": "maschile", "numero": "singolare"},
        "i": {"genere": "maschile", "numero": "plurale"},
        "gli": {"genere": "maschile", "numero": "plurale"},
        "le": {"genere": "femminile", "numero": "plurale"},
        "un": {"genere": "maschile", "numero": "singolare"},
        "uno": {"genere": "maschile", "numero": "singolare"},
        "una": {"genere": "femminile", "numero": "singolare"},
    }
    noun_features = {
        "bambina": {"genere": "femminile", "numero": "singolare"},
        "libro": {"genere": "maschile", "numero": "singolare"},
        "scolapiatti": {"genere": "maschile", "numero": "singolare"},
    }
    verb_features = {
        "legge": {"lemma": "leggere"},
        "devi": {"lemma": "dovere", "modo": "indicativo", "tempo": "presente", "persona": "seconda", "numero": "singolare"},
        "ordinare": {"lemma": "ordinare"},
    }

    if cat in {"articolo_determinativo", "articolo_indeterminativo"}:
        out.update(article_features.get(low, {}))
    if cat == "nome_comune":
        out.update(noun_features.get(low, {}))
    if cat == "verbo":
        out.update(verb_features.get(low, {}))
        if "lemma" not in out and low.endswith(("are","ere","ire")):
            out["lemma"] = low
    return out

def _enrich_analysis(arr):
    if not isinstance(arr, list):
        return arr
    out = []
    for item in arr:
        if not isinstance(item, dict):
            out.append(item); continue
        merged = dict(item)
        for k, v in _infer_item_fields(merged).items():
            if not str(merged.get(k) or "").strip():
                merged[k] = v
        out.append(merged)
    return out
'''

def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()

def _ensure_workspace_file(target: dict, ws: str, rel: str) -> Path:
    ws_path = Path(ws)
    src = BASE / rel
    dst = ws_path / rel
    dst.parent.mkdir(parents=True, exist_ok=True)
    if not dst.exists() and src.exists():
        shutil.copy2(src, dst)

    pkg_init_src = BASE / "openshell_backend" / "__init__.py"
    pkg_init_dst = ws_path / "openshell_backend" / "__init__.py"
    pkg_init_dst.parent.mkdir(parents=True, exist_ok=True)
    if pkg_init_src.exists() and not pkg_init_dst.exists():
        shutil.copy2(pkg_init_src, pkg_init_dst)

    return dst

def _patch_grammar_file(path: Path):
    if not path.exists():
        return False, f"missing workspace file: {path}"
    s = path.read_text(encoding="utf-8", errors="replace")
    orig = s

    anchor = "def _simple_local_grammar_fallback(phrase: str) -> list[dict[str, Any]] | None:\n"
    if "_infer_item_fields(" not in s:
        if anchor not in s:
            return False, "grammar helper anchor not found"
        s = s.replace(anchor, GRAMMAR_HELPERS + "\n\n" + anchor, 1)

    s = s.replace(
'''        arr = _extract_json_array(data.get("response") or "")
        if not _validate_grammar_output(phrase, arr):
            return None
        return arr
''',
'''        arr = _extract_json_array(data.get("response") or "")
        arr = _enrich_analysis(arr)
        if not _validate_grammar_output(phrase, arr):
            return None
        return arr
''', 1)

    s = s.replace(
'''    if punct:
        out.append({"token": punct, "categoria": "segno_punteggiatura"})

    return out
''',
'''    if punct:
        out.append({"token": punct, "categoria": "segno_punteggiatura"})

    out = _enrich_analysis(out)
    return out
''', 1)

    s = s.replace(
'''Usa SOLO le categorie del manuale e restituisci SOLO un array JSON.
Nessuna spiegazione fuori dal JSON.
''',
'''Usa SOLO le categorie del manuale e restituisci SOLO un array JSON.
Nessuna spiegazione fuori dal JSON.

Per i verbi aggiungi, quando inferibili: lemma, modo, tempo, persona, numero.
Per articoli e nomi aggiungi, quando inferibili: genere, numero.
''', 1)

    if s == orig:
        return False, "no changes applied"
    path.write_text(s, encoding="utf-8")
    return True, f"patched {path.name}"

def apply_fix_plan(ws: str, target: dict, fix_plan: dict) -> dict:
    name = str(target.get("name") or "").strip().lower()
    if name != "grammar":
        return {"ok": False, "changed_files": [], "summary": f"no coder for target {name!r}", "before_hashes": {}, "after_hashes": {}}
    for rel in target.get("patch_files", []) or []:
        if rel.endswith("skill_grammar_rag.py"):
            p = _ensure_workspace_file(target, ws, rel)
            before = _sha256(p) if p.exists() else ""
            ok, msg = _patch_grammar_file(p)
            after = _sha256(p) if p.exists() else ""
            return {
                "ok": ok,
                "changed_files": [str(p)] if ok else [],
                "summary": msg,
                "before_hashes": {str(p): before} if before else {},
                "after_hashes": {str(p): after} if after else {},
                "fix_plan": fix_plan,
            }
    return {"ok": False, "changed_files": [], "summary": "grammar patch file not configured", "before_hashes": {}, "after_hashes": {}}
