from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
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

    def timeline_facts(self) -> list[str]:
        parsed: list[tuple[SkeletonEntry, datetime]] = []
        for entry in self.entries:
            if not entry.timestamp:
                continue
            try:
                parsed.append((entry, datetime.fromisoformat(entry.timestamp)))
            except ValueError:
                continue
        base = min((stamp for _, stamp in parsed), default=None)
        output = [f"T0={base.isoformat(timespec='minutes')}"] if base else []
        parsed_by_id = {id(entry): stamp for entry, stamp in parsed}
        for entry in self.entries:
            stamp = parsed_by_id.get(id(entry))
            if base is not None and stamp is not None:
                delta = int((stamp - base).total_seconds() // 60)
                output.append(f"+{delta}m {entry.text}")
            elif entry.timestamp:
                output.append(f"{entry.timestamp} {entry.text}")
            else:
                output.append(entry.text)
        return output


GrammarAnalysisMap = Mapping[str, list[dict[str, Any]]]
ValencyMap = Mapping[str, tuple[str, ...]]
GrammarSessionFactory = Callable[[], Any]

_AUX_LEMMAS = frozenset({"essere", "avere"})
_MODAL_LEMMAS = frozenset({"dovere", "potere"})
_FINITE_MODES = frozenset({"indicativo", "congiuntivo", "condizionale", "imperativo"})
_ROLE_PREFIX_RE = re.compile(r"^([UAST?])>\s*", re.I)


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


def _verb_lemmas(analyses: GrammarAnalysisMap) -> tuple[str, ...]:
    output: list[str] = []
    for rows in analyses.values():
        for row in rows:
            if str(row.get("category") or "") != "verbo":
                continue
            lemma = str((row.get("features") or {}).get("lemma") or "").strip().casefold()
            if lemma and lemma not in output:
                output.append(lemma)
    return tuple(output)


def lookup_valency(
    analyses: GrammarAnalysisMap,
    *,
    session_factory: GrammarSessionFactory | None = None,
    max_lemmas: int = 24,
) -> dict[str, tuple[str, ...]]:
    lemmas = _verb_lemmas(analyses)[:max_lemmas]
    if not lemmas:
        return {}
    output: dict[str, tuple[str, ...]] = {}
    try:
        with (session_factory or _default_grammar_session)() as client:
            names = {tool.name for tool in client.list_tools()}
            if "grammar.lookup_valency" not in names:
                return {}
            for lemma in lemmas:
                payload = client.call_tool("grammar.lookup_valency", {"lemma": lemma})
                content = payload.get("structuredContent") or {}
                patterns: list[str] = []
                for row in content.get("tpas") or ():
                    if isinstance(row, Mapping) and row.get("pattern"):
                        patterns.append(str(row["pattern"]))
                for row in content.get("frames") or ():
                    if isinstance(row, Mapping) and row.get("valency"):
                        patterns.append(str(row["valency"]))
                if patterns:
                    output[lemma] = tuple(patterns[:4])
    except Exception:
        return {}
    return output


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


def _verb_rows(token: str, analyses: GrammarAnalysisMap) -> list[dict[str, Any]]:
    return [
        row for row in analyses.get(token.casefold(), [])
        if str(row.get("category") or "") == "verbo"
    ]


def _verb_candidate(token: str, analyses: GrammarAnalysisMap) -> tuple[str, bool] | None:
    rows = _verb_rows(token, analyses)
    candidates: list[tuple[str, bool]] = []
    for row in rows:
        features = row.get("features") or {}
        lemma = str(features.get("lemma") or "").strip().casefold()
        if not lemma:
            continue
        finite = str(features.get("modo") or "") in _FINITE_MODES
        candidates.append((lemma, finite))
    if not candidates:
        return None
    finite = [item for item in candidates if item[1]]
    pool = finite or candidates
    non_aux = [item for item in pool if item[0] not in _AUX_LEMMAS]
    return (non_aux or pool)[0]


def _dense_token(token: str, analyses: GrammarAnalysisMap) -> str:
    key = token.casefold()
    if key in CRITICAL_WORDS:
        return key
    rows = analyses.get(key) or []
    for row in rows:
        if str(row.get("category") or "") != "verbo":
            continue
        features = row.get("features") or {}
        if str(features.get("modo") or "") in {"condizionale", "congiuntivo", "imperativo", "participio"}:
            return token
        if str(features.get("tempo") or "") in {"futuro", "passato", "imperfetto"}:
            return token
    lemma = _lemma_for(token, analyses)
    return lemma.casefold() if lemma and token[:1].islower() else token


def _has_determiner_reading(token: str, analyses: GrammarAnalysisMap) -> bool:
    categories = {str(row.get("category") or "") for row in analyses.get(token.casefold(), [])}
    return any(
        category.startswith("articolo_") or category.startswith("aggettivo_dimostrativo")
        or category.startswith("aggettivo_possessivo") or category.startswith("aggettivo_indefinito")
        for category in categories
    )


def _candidate_is_nominally_blocked(index: int, tokens: list[str], analyses: GrammarAnalysisMap) -> bool:
    token = tokens[index]
    rows = analyses.get(token.casefold()) or []
    categories = {str(row.get("category") or "") for row in rows}
    if "verbo" not in categories or not (categories - {"verbo"}):
        return False
    if index <= 0:
        return False
    return _has_determiner_reading(tokens[index - 1], analyses)


def _has_finite(tokens: list[str], analyses: GrammarAnalysisMap) -> bool:
    for index, token in enumerate(tokens):
        candidate = _verb_candidate(token, analyses)
        if candidate and candidate[1] and not _candidate_is_nominally_blocked(index, tokens, analyses):
            return True
    return False


def _split_dense_clauses(tokens: list[str], analyses: GrammarAnalysisMap) -> list[tuple[str, list[str]]]:
    pieces: list[tuple[str, list[str]]] = []
    current: list[str] = []
    connector = ""
    for index, token in enumerate(tokens):
        key = token.casefold()
        rest = tokens[index + 1:]
        if not current and key in {"e", "ma", "mentre", "perché", "perche"}:
            connector = key if key != "e" else ";"
            continue
        separator = key in {"e", "ma", "mentre", "perché", "perche"} or token in {",", ";", ".", "!", "?"}
        if separator and current and _has_finite(current, analyses) and _has_finite(rest, analyses):
            pieces.append((connector, current))
            connector = key if key in {"ma", "mentre", "perché", "perche"} else ";"
            current = []
            continue
        if token not in {".", "!", "?"}:
            current.append(token)
    if current:
        pieces.append((connector, current))
    return pieces or [("", tokens)]


def _dense_clause(tokens: list[str], analyses: GrammarAnalysisMap, valency: ValencyMap) -> str:
    if not tokens:
        return ""
    if len(tokens) <= 3 and tokens[0].casefold() == "va" and any(t.casefold() in {"bene", "male"} for t in tokens[1:]):
        return " ".join(tokens)
    candidates: list[tuple[int, str, bool]] = []
    for index, token in enumerate(tokens):
        candidate = _verb_candidate(token, analyses)
        if candidate and not _candidate_is_nominally_blocked(index, tokens, analyses):
            candidates.append((index, candidate[0], candidate[1]))
    finite = [item for item in candidates if item[2]]
    preferred = [item for item in finite if item[1] not in _AUX_LEMMAS]
    main = preferred or finite or [item for item in candidates if item[1] not in _AUX_LEMMAS] or candidates
    if not main:
        return compact_text(" ".join(tokens), analyses)
    main_index, main_lemma, _ = main[0]
    if main_lemma not in _AUX_LEMMAS and main_lemma not in _MODAL_LEMMAS and not valency.get(main_lemma):
        return compact_text(" ".join(tokens), analyses)

    def keep(index: int, token: str) -> str | None:
        key = token.casefold()
        if index == main_index or token in {".", ",", ";", ":", "!", "?", "(", ")", "[", "]", "{", "}"}:
            return None
        if _drop_token(token, analyses) or key == "e":
            return None
        candidate = _verb_candidate(token, analyses)
        if candidate and candidate[0] in _AUX_LEMMAS and main_lemma not in _AUX_LEMMAS:
            return None
        return _dense_token(token, analyses)

    left = [value for i, token in enumerate(tokens[:main_index]) if (value := keep(i, token))]
    right = [value for i, token in enumerate(tokens[main_index + 1:], start=main_index + 1) if (value := keep(i, token))]
    negated = "non" in left[-2:]
    if main_lemma == "essere" and left and right:
        if negated:
            left = [item for item in left if item != "non"]
            return " ".join(left) + "!=" + " ".join(right)
        return " ".join(left) + "=" + " ".join(right)
    predicate = tokens[main_index].casefold() if main_lemma in _MODAL_LEMMAS else main_lemma
    if negated:
        left = [item for item in left if item != "non"]
        predicate = "non " + predicate
    args = " ".join(part for part in (" ".join(left), " ".join(right)) if part)
    return predicate + (" " + args if args else "")


def dense_compact_text(
    text: str,
    analyses: GrammarAnalysisMap,
    valency: ValencyMap | None = None,
) -> str:
    """Research-only predicate skeleton for old context; ambiguity falls back to surface-safe text."""
    match = _ROLE_PREFIX_RE.match(text)
    role = (match.group(1).upper() + ">") if match else ""
    body = text[match.end():] if match else text
    tokens = tokenize(body)
    pieces = _split_dense_clauses(tokens, analyses)
    rendered: list[str] = []
    for connector, clause in pieces:
        value = _dense_clause(clause, analyses, valency or {})
        if not value:
            continue
        if connector and connector != ";":
            rendered.append(connector)
        rendered.append(value)
    return role + ";".join(rendered)


def _dedupe_key(entry: SkeletonEntry) -> tuple[str, str, str]:
    normalized = " ".join(entry.text.casefold().split())
    return entry.kind[:1].upper(), entry.certainty[:1], normalized


def compact_context(
    segments: Iterable[ContextSegment],
    *,
    use_grammar: bool = False,
    grammar_session_factory: GrammarSessionFactory | None = None,
    max_unique_grammar_tokens: int = 128,
    profile: str = "safe",
) -> SemanticSkeleton:
    source = tuple(segments)
    texts = [item.text for item in source]
    if profile not in {"safe", "dense"}:
        raise ValueError("semantic_skeleton_profile_invalid")
    want_grammar = use_grammar or profile == "dense"
    grammar = (
        lookup_grammar(
            texts,
            session_factory=grammar_session_factory,
            max_unique_tokens=max_unique_grammar_tokens,
        )
        if want_grammar else {}
    )
    valency = (
        lookup_valency(grammar, session_factory=grammar_session_factory)
        if profile == "dense" and grammar else {}
    )
    merged: dict[tuple[str, str, str], SkeletonEntry] = {}
    for item in source:
        if item.exact:
            compact = " ".join(item.text.split())
        elif profile == "dense":
            compact = dense_compact_text(item.text, grammar, valency)
        else:
            compact = compact_text(item.text, grammar)
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
    "conversation_segments", "dense_compact_text", "judge_case_segments",
    "lookup_grammar", "lookup_valency", "tokenize",
]
