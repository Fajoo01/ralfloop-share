from __future__ import annotations

from dataclasses import dataclass, field
import json
import re
from typing import Any, Callable, Iterable, Mapping


DEFAULT_GRAMMAR_SOCKET = "/run/ralf-teacher-grammar/grammar.sock"

# Small words that carry judge-critical meaning must never be pruned.
CRITICAL_WORDS = frozenset({
    "non", "mai", "senza", "solo", "solamente", "soltanto",
    "se", "salvo", "eccetto", "tranne", "oppure", "o",
    "prima", "dopo", "entro", "durante", "finché", "finche",
    "almeno", "massimo", "minimo", "tutti", "tutto", "nessuno",
    "ogni", "qualsiasi", "già", "gia", "ancora", "sempre",
    "deve", "devono", "dovere", "può", "puo", "possono", "potere",
    "vietato", "obbligatorio", "obbligatoria", "necessario", "necessaria",
})

_DROP_CATEGORIES = frozenset({"articolo_determinativo", "articolo_indeterminativo"})
_TOKEN_RE = re.compile(
    r"https?://[^\s]+|[\w.+-]+@[\w.-]+|\b\d+(?:[.,:/-]\d+)*\b|[A-Za-zÀ-ÿ][\wÀ-ÿ'’-]*|[^\w\s]",
    re.UNICODE,
)

@dataclass(frozen=True)
class ContextSegment:
    text: str
    ref: str
    kind: str = "fact"
    certainty: str = "A"
    timestamp: str | None = None
    source: str | None = None
    priority: int = 0
    exact: bool = False


@dataclass
class SkeletonEntry:
    kind: str
    certainty: str
    text: str
    refs: list[str] = field(default_factory=list)
    timestamp: str | None = None
    source: str | None = None
    priority: int = 0

    def wire(self) -> list[Any]:
        row: list[Any] = [self.kind[:1].upper(), self.certainty[:1], self.text]
        if self.refs:
            row.append(",".join(self.refs[:3]))
        if self.timestamp:
            row.append(self.timestamp)
        return row


@dataclass(frozen=True)
class SemanticSkeleton:
    entries: tuple[SkeletonEntry, ...]
    original_chars: int
    compact_chars: int
    grammar_tokens: int = 0
    grammar_hits: int = 0

    @property
    def ratio(self) -> float:
        if not self.original_chars:
            return 1.0
        return self.compact_chars / self.original_chars

    def packet(self) -> dict[str, Any]:
        return {"v": 1, "E": [entry.wire() for entry in self.entries]}

    def wire(self) -> str:
        return json.dumps(self.packet(), ensure_ascii=False, separators=(",", ":"))


GrammarAnalysisMap = Mapping[str, list[dict[str, Any]]]
GrammarSessionFactory = Callable[[], Any]


def tokenize(text: str) -> list[str]:
    return _TOKEN_RE.findall(str(text or ""))


def _default_grammar_session() -> Any:
    from src.mcp_transport import MCPClientSession, UnixMCPTransport

    return MCPClientSession(
        UnixMCPTransport(DEFAULT_GRAMMAR_SOCKET, connect_timeout=0.5),
        timeout=2.0,
        client_name="bottazzi-motor-semantic-skeleton",
    )


def lookup_grammar(
    texts: Iterable[str],
    *,
    session_factory: GrammarSessionFactory | None = None,
    max_unique_tokens: int = 128,
) -> dict[str, list[dict[str, Any]]]:
    words: list[str] = []
    seen: set[str] = set()
    for text in texts:
        for token in tokenize(text):
            key = token.casefold()
            if not token[:1].isalpha() or key in seen:
                continue
            seen.add(key)
            words.append(token)
            if len(words) >= max_unique_tokens:
                break
        if len(words) >= max_unique_tokens:
            break
    if not words:
        return {}
    results: dict[str, list[dict[str, Any]]] = {}
    try:
        with (session_factory or _default_grammar_session)() as client:
            names = {tool.name for tool in client.list_tools()}
            if "grammar.lookup_token" not in names:
                return {}
            for token in words:
                payload = client.call_tool("grammar.lookup_token", {"token": token})
                content = payload.get("structuredContent") or {}
                analyses = content.get("analyses") or []
                if isinstance(analyses, list):
                    results[token.casefold()] = [
                        dict(item) for item in analyses if isinstance(item, Mapping)
                    ]
    except Exception:
        return {}
    return results


def _lemma_for(token: str, analyses: GrammarAnalysisMap) -> str | None:
    rows = analyses.get(token.casefold()) or []
    if not rows:
        return None
    if any(str(row.get("category")) == "nome_proprio" for row in rows):
        return None
    lemmas = {
        str((row.get("features") or {}).get("lemma") or "").strip()
        for row in rows
    } - {""}
    return next(iter(lemmas)) if len(lemmas) == 1 else None


def _drop_token(token: str, analyses: GrammarAnalysisMap) -> bool:
    rows = analyses.get(token.casefold()) or []
    if not rows:
        return False
    categories = {str(row.get("category") or "") for row in rows}
    return bool(categories) and categories <= _DROP_CATEGORIES


def compact_text(text: str, analyses: GrammarAnalysisMap | None = None) -> str:
    grammar = analyses or {}
    output: list[str] = []
    for token in tokenize(text):
        key = token.casefold()
        if token in {".", ",", ";", ":", "!", "?", "(", ")", "[", "]", "{", "}"}:
            continue
        if key in CRITICAL_WORDS:
            output.append(key)
            continue
        if _drop_token(token, grammar):
            continue
        lemma = _lemma_for(token, grammar)
        if lemma and token[:1].islower():
            output.append(lemma.casefold())
        else:
            output.append(token)
    return " ".join(output)


def _dedupe_key(entry: SkeletonEntry) -> tuple[str, str, str]:
    normalized = " ".join(entry.text.casefold().split())
    return entry.kind[:1].upper(), entry.certainty[:1], normalized


def compact_context(
    segments: Iterable[ContextSegment],
    *,
    use_grammar: bool = False,
    grammar_session_factory: GrammarSessionFactory | None = None,
    max_unique_grammar_tokens: int = 128,
) -> SemanticSkeleton:
    source = tuple(segments)
    texts = [item.text for item in source]
    grammar = (
        lookup_grammar(
            texts,
            session_factory=grammar_session_factory,
            max_unique_tokens=max_unique_grammar_tokens,
        )
        if use_grammar else {}
    )
    merged: dict[tuple[str, str, str], SkeletonEntry] = {}
    for item in source:
        compact = " ".join(item.text.split()) if item.exact else compact_text(item.text, grammar)
        entry = SkeletonEntry(
            kind=item.kind,
            certainty=item.certainty,
            text=compact,
            refs=[item.ref] if item.ref else [],
            timestamp=item.timestamp,
            source=item.source,
            priority=item.priority,
        )
        key = _dedupe_key(entry)
        previous = merged.get(key)
        if previous is None:
            merged[key] = entry
        else:
            previous.refs.extend(ref for ref in entry.refs if ref not in previous.refs)
            if entry.timestamp and (not previous.timestamp or entry.timestamp > previous.timestamp):
                previous.timestamp = entry.timestamp
            previous.priority = max(previous.priority, entry.priority)
    entries = tuple(sorted(
        merged.values(),
        key=lambda item: (-item.priority, item.timestamp or "", item.kind, item.text),
    ))
    original_chars = sum(len(item.text) for item in source)
    packet = json.dumps(
        {"v": 1, "E": [entry.wire() for entry in entries]},
        ensure_ascii=False,
        separators=(",", ":"),
    )
    grammar_hits = sum(1 for rows in grammar.values() if rows)
    return SemanticSkeleton(
        entries=entries,
        original_chars=original_chars,
        compact_chars=len(packet),
        grammar_tokens=len(grammar),
        grammar_hits=grammar_hits,
    )


def judge_case_segments(case: Any, extra: Iterable[ContextSegment] = ()) -> tuple[ContextSegment, ...]:
    segments = [ContextSegment(str(case.goal), "goal", kind="goal", priority=100, exact=True)]
    segments.extend(
        ContextSegment(str(text), f"fact:{index}", kind="fact", priority=70)
        for index, text in enumerate(case.facts)
    )
    segments.extend(
        ContextSegment(str(text), f"rule:{index}", kind="rule", priority=90, exact=True)
        for index, text in enumerate(case.rules)
    )
    if getattr(case, "candidate_answer", None):
        segments.append(ContextSegment(str(case.candidate_answer), "candidate", kind="answer", priority=50))
    segments.extend(extra)
    return tuple(segments)


def conversation_segments(
    turns: Iterable[Mapping[str, Any]],
    *,
    recent_exact: int = 2,
) -> tuple[ContextSegment, ...]:
    rows = tuple(turns)
    cutoff = max(0, len(rows) - max(0, recent_exact))
    role_code = {"user": "U", "assistant": "A", "system": "S", "tool": "T"}
    output: list[ContextSegment] = []
    for index, row in enumerate(rows):
        role = str(row.get("role") or "unknown").casefold()
        content = str(row.get("content") or "")
        if not content.strip():
            continue
        prefix = role_code.get(role, "?")
        output.append(ContextSegment(
            text=f"{prefix}>{content}",
            ref=str(row.get("id") or f"turn:{index}"),
            kind="history",
            certainty="?",
            timestamp=str(row.get("timestamp") or "") or None,
            source=role,
            priority=30 + (10 if index >= cutoff else 0),
            exact=index >= cutoff,
        ))
    return tuple(output)


def compact_judge_case(case: Any, **kwargs: Any) -> SemanticSkeleton:
    return compact_context(judge_case_segments(case), **kwargs)


__all__ = [
    "CRITICAL_WORDS", "ContextSegment", "DEFAULT_GRAMMAR_SOCKET", "SemanticSkeleton",
    "SkeletonEntry", "compact_context", "compact_judge_case", "compact_text",
    "conversation_segments", "judge_case_segments", "lookup_grammar", "tokenize",
]
