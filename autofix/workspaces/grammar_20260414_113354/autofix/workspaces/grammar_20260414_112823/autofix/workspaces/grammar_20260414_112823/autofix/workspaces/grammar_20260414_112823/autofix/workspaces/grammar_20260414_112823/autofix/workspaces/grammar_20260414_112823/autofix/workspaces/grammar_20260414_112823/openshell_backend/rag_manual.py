from __future__ import annotations

import re

import requests

CHESHIRE_BASE_URL = "http://127.0.0.1:1865"

def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").lower()).strip()

def _tokenize(text: str) -> set[str]:
    return set(re.findall(r"\w+", _normalize(text), flags=re.UNICODE))

def _score(query_tokens: set[str], text: str) -> tuple[int, int]:
    toks = _tokenize(text)
    return (len(query_tokens & toks), len(text or ""))

def retrieve_manual_snippets(collection: str, query: str, max_chars: int = 5000) -> str:
    collection = str(collection or "").strip()
    query = str(query or "").strip()

    if not collection or not query:
        return ""

    try:
        r = requests.get(
            f"{CHESHIRE_BASE_URL}/memory/collections/declarative/points",
            params={"limit": 1000},
            timeout=30,
        )
        r.raise_for_status()
        data = r.json()
    except Exception:
        return ""

    points = data.get("points", []) or []
    query_tokens = _tokenize(query)
    ranked = []

    for p in points:
        payload = p.get("payload", {}) or {}
        metadata = payload.get("metadata", {}) or {}
        page_content = str(payload.get("page_content") or "").strip()

        if not page_content:
            continue

        if str(metadata.get("collection") or "").strip() != collection:
            continue

        if str(metadata.get("tag") or "").strip() != "grammar_manual":
            continue

        sc = _score(query_tokens, page_content)
        if sc[0] <= 0:
            continue

        source = str(metadata.get("source_file") or metadata.get("source") or "unknown")
        ranked.append((sc, source, page_content))

    ranked.sort(key=lambda x: x[0], reverse=True)

    out = []
    used = 0
    for _, source, text in ranked:
        block = f"[SOURCE: {source}]\n{text}".strip()
        extra = len(block) + (2 if out else 0)
        if used + extra > max_chars:
            continue
        out.append(block)
        used += extra
        if used >= max_chars:
            break

    return "\n\n".join(out).strip()
