from __future__ import annotations

from pathlib import Path
import re

RAG_BASE = Path("/home/sibilla-cumana/ralfloop_agent_scaffold/openshell_backend/rag_manuals")


def _tokenize(text: str) -> list[str]:
    return re.findall(r"\w+|[^\w\s]", (text or "").lower(), flags=re.UNICODE)


def retrieve_manual_snippets(collection: str, query: str, max_chars: int = 5000) -> str:
    root = RAG_BASE / collection
    if not root.exists():
        return ""

    q_tokens = set(_tokenize(query))
    scored: list[tuple[int, Path, str]] = []

    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except Exception:
            continue

        tokens = set(_tokenize(text))
        score = len(q_tokens & tokens)
        scored.append((score, path, text))

    scored.sort(key=lambda x: (x[0], str(x[1])), reverse=True)

    picked: list[str] = []
    used = 0
    for score, path, text in scored:
        if score <= 0 and picked:
            break
        block = f"\n\n## SOURCE: {path.name}\n{text.strip()}\n"
        if used + len(block) > max_chars:
            break
        picked.append(block)
        used += len(block)

    return "".join(picked).strip()
