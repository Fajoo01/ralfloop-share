from __future__ import annotations

import json
import re
from typing import Any
from openshell_backend.grammar_validator import find_grammar_suspicions
import requests

from openshell_backend.rag_manual import retrieve_manual_snippets

OLLAMA_BASE_URL = "http://127.0.0.1:11434"
DEFAULT_MODEL = "qwen2.5:7b"
GRAMMAR_COLLECTION = "grammatica_italiana"

_ALLOWED = {
    "articolo_determinativo",
    "articolo_indeterminativo",
    "nome_comune",
    "nome_proprio",
    "aggettivo_qualificativo",
    "pronome",
    "preposizione",
    "preposizione_articolata",
    "congiunzione",
    "avverbio",
    "verbo",
    "interiezione",
    "numerale",
    "segno_punteggiatura",
}


def _extract_json_array(text: str) -> list[dict[str, Any]] | None:
    text = (text or "").strip()
    if not text:
        return None

    if text.startswith("```json"):
        text = text[len("```json"):].strip()
    elif text.startswith("```"):
        text = text[len("```"):].strip()
    if text.endswith("```"):
        text = text[:-3].strip()

    try:
        obj = json.loads(text)
        return obj if isinstance(obj, list) else None
    except Exception:
        pass

    m = re.search(r".*", text, flags=re.DOTALL)
    if not m:
        return None

    try:
        obj = json.loads(m.group(0))
        return obj if isinstance(obj, list) else None
    except Exception:
        return None


def _tokenize_phrase(phrase: str) -> list[str]:
    return re.findall(r"\w+|[^\w\s]", (phrase or "").strip(), flags=re.UNICODE)


def _validate_grammar_output(phrase: str, arr: list[dict[str, Any]] | None) -> bool:
    if not isinstance(arr, list) or not arr:
        return False

    expected_tokens = _tokenize_phrase(phrase)
    got_tokens: list[str] = []

    for item in arr:
        if not isinstance(item, dict):
            return False
        token = str(item.get("token") or "")
        categoria = str(item.get("categoria") or "")
        if not token or not categoria:
            return False
        if categoria not in _ALLOWED:
            return False
        got_tokens.append(token)

    return got_tokens == expected_tokens


def analyze_grammar_with_rag(phrase: str, model: str = DEFAULT_MODEL) -> list[dict[str, Any]] | None:
    manual = retrieve_manual_snippets(GRAMMAR_COLLECTION, phrase)
    if not manual:
        return None

    prompt = f"""Sei un analizzatore grammaticale italiano.

Usa SOLO le categorie del manuale e restituisci SOLO un array JSON.
Nessuna spiegazione fuori dal JSON.

Per i verbi aggiungi, quando inferibili: lemma, modo, tempo, persona, numero.
Per articoli e nomi aggiungi, quando inferibili: genere, numero.

MANUALE:
{manual}

FRASE:
{phrase}
"""

    try:
        r = requests.post(
            f"{OLLAMA_BASE_URL}/api/generate",
            json={
                "model": model,
                "prompt": prompt,
                "stream": False,
                "options": {
                    "temperature": 0,
                    "top_p": 1,
                    "repeat_penalty": 1
                }
            },
            timeout=90,
        )
        r.raise_for_status()
        data = r.json()
        arr = _extract_json_array(data.get("response") or "")
        arr = _enrich_analysis(arr)
        if not _validate_grammar_output(phrase, arr):
            return None
        return arr
    except Exception:
        return None





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



def _autofix_apostrophe_token(tok: str) -> list[dict[str, Any]] | None:
    t = (tok or "").strip()
    if not t or "'" not in t:
        return None

    low = t.lower()

    prep_art_prefixes = (
        "dall'", "all'", "nell'", "sull'", "coll'",
        "dell'", "agl'", "dagl'", "negl'", "sugl'",
    )
    art_prefixes = ("l'", "un'")

    for pref in prep_art_prefixes:
        if low.startswith(pref) and len(t) > len(pref):
            head = t[:len(pref)]
            tail = t[len(pref):]
            if tail and tail[0].isupper():
                return [
                    {"token": head, "categoria": "preposizione_articolata"},
                    {"token": tail, "categoria": "nome_proprio"},
                ]
            return [
                {"token": head, "categoria": "preposizione_articolata"},
                {"token": tail, "categoria": "nome_comune"},
            ]

    for pref in art_prefixes:
        if low.startswith(pref) and len(t) > len(pref):
            head = t[:len(pref)]
            tail = t[len(pref):]
            if pref == "l'":
                head_cat = "articolo_determinativo"
            else:
                head_cat = "articolo_indeterminativo"
            if tail and tail[0].isupper():
                return [
                    {"token": head, "categoria": head_cat},
                    {"token": tail, "categoria": "nome_proprio"},
                ]
            return [
                {"token": head, "categoria": head_cat},
                {"token": tail, "categoria": "nome_comune"},
            ]

    return None



def _is_suspicious_grammar_item(item: dict[str, Any]) -> bool:
    tok = str(item.get("token") or "")
    cat = str(item.get("categoria") or "")
    if "'" in tok and cat == "nome_comune":
        low = tok.lower()
        suspicious_prefixes = (
            "dall'", "all'", "nell'", "sull'", "coll'",
            "dell'", "agl'", "dagl'", "negl'", "sugl'",
            "l'", "un'",
        )
        return low.startswith(suspicious_prefixes)
    return False


def _repair_suspicious_analysis(arr: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    changed = False

    for item in arr:
        if not isinstance(item, dict):
            out.append(item)
            continue

        if _is_suspicious_grammar_item(item):
            tok = str(item.get("token") or "")
            fixed = _apply_autocorrect_rules(tok) or _autofix_apostrophe_token(tok)
            if fixed:
                out.extend(fixed)
                changed = True
                continue

        out.append(item)

    if changed:
        out = _enrich_analysis(out)
    return out


def _simple_local_grammar_fallback(phrase: str) -> list[dict[str, Any]] | None:
    p = (phrase or "").strip()
    if not p:
        return None

    punct = ""
    if p and p[-1] in ".!?":
        punct = p[-1]
        p = p[:-1].rstrip()

    tokens = p.split()
    if not tokens:
        return None

    out: list[dict[str, Any]] = []

    def add(tok: str, cat: str):
        out.append({"token": tok, "categoria": cat})

    articles_det = {"il","lo","la","i","gli","le","l'"}
    articles_indet = {"un","uno","una","un'"}
    prep_art = {
        "del","dello","della","dei","degli","delle",
        "al","allo","alla","ai","agli","alle",
        "dal","dallo","dalla","dai","dagli","dalle",
        "nel","nello","nella","nei","negli","nelle",
        "sul","sullo","sulla","sui","sugli","sulle",
    }
    simple_preps = {"di","a","da","in","con","su","per","tra","fra"}
    pronouns = {"io","tu","egli","elli","lui","lei","noi","voi","essi","esse","loro","mi","ti","si","ci","vi"}
    modal_verbs = {"devo","devi","deve","dobbiamo","dovete","devono"}
    common_verbs = {
        "leggo","leggi","legge","leggiamo","leggete","leggono",
        "ordinare","ordino","ordini","ordina","ordiniamo","ordinate","ordinano",
        "devo","devi","deve","dobbiamo","dovete","devono",
    }

    for tok in tokens:
        low = tok.lower()

        if low in articles_det:
            add(tok, "articolo_determinativo")
        elif low in articles_indet:
            add(tok, "articolo_indeterminativo")
        elif low in prep_art:
            add(tok, "preposizione_articolata")
        elif low in simple_preps:
            add(tok, "preposizione")
        elif low in pronouns:
            add(tok, "pronome")
        elif low in modal_verbs or low in common_verbs or low.endswith(("are","ere","ire")):
            add(tok, "verbo")
        else:
            fixed = _autofix_apostrophe_token(tok)
            if fixed:
                out.extend(fixed)
            else:
                add(tok, "nome_comune")

    if punct:
        out.append({"token": punct, "categoria": "segno_punteggiatura"})

    out = _enrich_analysis(out)
    return out

def analyze_grammar_with_diagnostics(phrase: str) -> dict[str, Any] | None:
    arr = _simple_local_grammar_fallback(phrase)
    if not arr:
        return None
    return {
        "items": arr,
        "issues": find_grammar_suspicions(arr),
    }


def maybe_answer_grammar_request(user_goal: str) -> str | None:
    text = (user_goal or "").strip()

    patterns = [
        r"analisi grammaticale(?: della frase)?(?: di)?\s*:?\s*(.+)$",
        r"fai l'analisi grammaticale(?: della frase)?(?: di)?\s*:?\s*(.+)$",
        r"analizza grammaticalmente(?: la frase)?(?: di)?\s*:?\s*(.+)$",
    ]

    m = None
    for pat in patterns:
        m = re.search(pat, text, flags=re.IGNORECASE)
        if m:
            break

    if not m:
        return None

    phrase = m.group(1).strip(" :")
    if not phrase:
        return None

    arr = analyze_grammar_with_rag(phrase)
    if not arr:
        arr = _simple_local_grammar_fallback(phrase)
    if not arr:
        return None
    return json.dumps(arr, ensure_ascii=False)
