from __future__ import annotations

import json
import re
from typing import Any
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
        if not _validate_grammar_output(phrase, arr):
            return None
        return arr
    except Exception:
        return None


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
        return None
    return json.dumps(arr, ensure_ascii=False)
