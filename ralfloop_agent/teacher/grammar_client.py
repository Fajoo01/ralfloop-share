from __future__ import annotations

import re
import unicodedata
from typing import Any

from src.mcp_transport import MCPClientSession, UnixMCPTransport


TOOLS = {
    "grammar.lookup_token",
    "grammar.lookup_lemma",
    "grammar.lookup_valency",
    "grammar.health",
}
TOKEN_RE = re.compile(r"[^\W\d_][\w'’-]{0,63}", re.UNICODE)
_NONFINITE = {"infinito", "gerundio", "participio"}
_AUX_LEMMAS = {"essere", "avere"}
_SUBJECT_PRONOUNS: dict[str, tuple[str, str, str | None]] = {
    "io": ("prima", "singolare", None),
    "tu": ("seconda", "singolare", None),
    "lui": ("terza", "singolare", "maschile"),
    "lei": ("terza", "singolare", "femminile"),
    "noi": ("prima", "plurale", None),
    "voi": ("seconda", "plurale", None),
    "loro": ("terza", "plurale", None),
}
_META_HEADS = {"frase", "parola", "forma", "espressione", "costruzione"}


def _canonical_key(value: str) -> str:
    return unicodedata.normalize("NFC", str(value or "")).casefold().replace("’", "'")


def _surface_tokens(text: str) -> list[str]:
    normalized = unicodedata.normalize("NFC", text[:1200]).replace("’", "'")
    out: list[str] = []
    for token in TOKEN_RE.findall(normalized):
        token = token.replace("’", "'")
        if "'" in token and not token.endswith("'"):
            prefix, suffix = token.split("'", 1)
            if prefix and suffix:
                out.extend((_canonical_key(prefix + "'"), _canonical_key(suffix)))
                continue
        out.append(_canonical_key(token))
    return [token for token in out if token]


def _quoted_chunks(text: str) -> list[str]:
    value = unicodedata.normalize("NFC", text).replace("’", "'")
    chunks = re.findall(r'«([^»]{1,300})»|“([^”]{1,300})”|"([^"]{1,300})"', value)
    return [" ".join(part for part in group if part) for group in chunks]


def _pair_is_quoted(text: str, left: str, right: str) -> bool:
    target = f"{left} {right}"
    return any(target in " ".join(_surface_tokens(chunk)) for chunk in _quoted_chunks(text))


def _is_metalinguistic(sequence: list[str], pair_index: int) -> bool:
    window = sequence[max(0, pair_index - 3):pair_index]
    return any(token in _META_HEADS for token in window)


def _feature(analysis: dict[str, Any], key: str) -> str:
    return _canonical_key(str((analysis.get("features") or {}).get(key) or ""))


def _finite_auxiliary(analyses: list[dict[str, Any]]) -> str | None:
    for analysis in analyses:
        if analysis.get("category") != "verbo":
            continue
        lemma = _feature(analysis, "lemma")
        mood = _feature(analysis, "modo")
        if lemma in _AUX_LEMMAS and mood not in _NONFINITE:
            return lemma
    return None


def _infinitive_lemma(analyses: list[dict[str, Any]]) -> str | None:
    for analysis in analyses:
        if analysis.get("category") != "verbo":
            continue
        if _feature(analysis, "modo") == "infinito":
            lemma = _feature(analysis, "lemma")
            if lemma:
                return lemma
    return None


def _filtered_participles(
    forms: list[dict[str, Any]],
    *,
    auxiliary_lemma: str,
    subject: tuple[str, str, str | None] | None,
) -> list[str]:
    if not forms:
        return []
    candidates = forms
    if auxiliary_lemma == "essere" and subject is not None:
        _person, number, gender = subject
        numbered = [row for row in candidates if _feature(row, "numero") == number]
        if numbered:
            candidates = numbered
        if gender:
            gendered = [row for row in candidates if _feature(row, "genere") == gender]
            if gendered:
                candidates = gendered
    elif auxiliary_lemma == "avere":
        base = [
            row for row in candidates
            if _feature(row, "numero") == "singolare"
            and _feature(row, "genere") == "maschile"
        ]
        if base:
            candidates = base
    values: list[str] = []
    seen: set[str] = set()
    for row in candidates:
        token = str(row.get("token") or "").strip()
        key = _canonical_key(token)
        if token and key not in seen:
            values.append(token)
            seen.add(key)
    return values[:4]


def contextual_l2_checks(
    text: str,
    *,
    analyses_by_token: dict[str, list[dict[str, Any]]],
    participle_forms: dict[str, list[dict[str, Any]]] | None = None,
) -> dict[str, Any]:
    sequence = _surface_tokens(text)
    forms_by_lemma = participle_forms or {}
    issues: list[dict[str, Any]] = []
    ambiguous: list[dict[str, Any]] = []
    suppressed: list[dict[str, Any]] = []

    for index in range(len(sequence) - 1):
        left, right = sequence[index], sequence[index + 1]
        auxiliary_lemma = _finite_auxiliary(analyses_by_token.get(left, []))
        infinitive_lemma = _infinitive_lemma(analyses_by_token.get(right, []))
        if not auxiliary_lemma or not infinitive_lemma:
            continue

        subject_token = ""
        subject = None
        for candidate in reversed(sequence[max(0, index - 3):index]):
            if candidate in _SUBJECT_PRONOUNS:
                subject_token = candidate
                subject = _SUBJECT_PRONOUNS[candidate]
                break
        record = {
            "kind": "finite_auxiliary_plus_infinitive",
            "auxiliary": left,
            "auxiliary_lemma": auxiliary_lemma,
            "infinitive": right,
            "lemma": infinitive_lemma,
            "subject": subject_token if subject else "",
            "candidate_participles": _filtered_participles(
                forms_by_lemma.get(infinitive_lemma, []),
                auxiliary_lemma=auxiliary_lemma,
                subject=subject,
            ),
        }

        if _pair_is_quoted(text, left, right) or _is_metalinguistic(sequence, index):
            record["reason"] = "mentioned_form_not_asserted_usage"
            suppressed.append(record)
            continue

        if auxiliary_lemma == "essere" and left == "è" and subject is None:
            record["reason"] = "copular_infinitive_possible"
            ambiguous.append(record)
            continue

        record["reason"] = "compound_tense_requires_participle"
        issues.append(record)

    return {
        "policy": "morphology_candidates_plus_context_not_contextual_truth",
        "issues": issues,
        "ambiguous": ambiguous,
        "suppressed": suppressed,
    }


class GrammarEvidenceClient:
    def __init__(self, socket_path: str, *, timeout: float = 3.0) -> None:
        self.socket_path = socket_path
        self.timeout = timeout

    def _connection(self) -> MCPClientSession:
        return MCPClientSession(
            UnixMCPTransport(self.socket_path),
            timeout=self.timeout,
            client_name="teacher-grammar-evidence",
        )

    @staticmethod
    def _call(session: MCPClientSession, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        raw = session.call_tool(name, arguments)
        payload = raw.get("structuredContent")
        if not isinstance(payload, dict) or payload.get("ok") is not True:
            raise RuntimeError("grammar_mcp_invalid_result")
        if payload.get("writes") != 0 or payload.get("external_side_effects") != 0:
            raise RuntimeError("grammar_mcp_side_effect_contract_broken")
        return payload

    def health(self) -> dict[str, Any]:
        with self._connection() as session:
            surface = {tool.name for tool in session.list_tools()}
            if surface != TOOLS:
                raise RuntimeError("grammar_mcp_surface_mismatch")
            return self._call(session, "grammar.health", {})

    @staticmethod
    def _tokens(text: str, limit: int) -> list[str]:
        unique: list[str] = []
        seen: set[str] = set()
        for token in _surface_tokens(text):
            if token not in seen and len(unique) < limit:
                unique.append(token)
                seen.add(token)
        return unique

    def evidence_for(
        self,
        text: str,
        *,
        max_tokens: int = 10,
        max_verbs: int = 3,
    ) -> dict[str, Any] | None:
        tokens = self._tokens(text, max(1, min(max_tokens, 16)))
        if not tokens:
            return None
        with self._connection() as session:
            surface = {tool.name for tool in session.list_tools()}
            if surface != TOOLS:
                raise RuntimeError("grammar_mcp_surface_mismatch")
            analyses = []
            analyses_by_token: dict[str, list[dict[str, Any]]] = {}
            verb_lemmas: list[str] = []
            infinitive_lemmas: list[str] = []
            for token in tokens:
                item = self._call(session, "grammar.lookup_token", {"token": _canonical_key(token)})
                token_analyses = item.get("analyses") or []
                analyses_by_token[token] = token_analyses
                if not token_analyses:
                    continue
                analyses.append(item)
                for analysis in token_analyses:
                    if analysis.get("category") != "verbo":
                        continue
                    lemma = str((analysis.get("features") or {}).get("lemma") or "").strip()
                    if lemma and lemma not in verb_lemmas:
                        verb_lemmas.append(lemma)
                    if lemma and _feature(analysis, "modo") == "infinito" and lemma not in infinitive_lemmas:
                        infinitive_lemmas.append(lemma)

            participle_forms: dict[str, list[dict[str, Any]]] = {}
            for lemma in infinitive_lemmas[:4]:
                item = self._call(
                    session,
                    "grammar.lookup_lemma",
                    {"lemma": _canonical_key(lemma), "modo": "participio", "tempo": "passato"},
                )
                participle_forms[_canonical_key(lemma)] = list(item.get("forms") or [])

            contextual = contextual_l2_checks(
                text,
                analyses_by_token=analyses_by_token,
                participle_forms=participle_forms,
            )

            valency = []
            for lemma in verb_lemmas[: max(0, min(max_verbs, 4))]:
                item = self._call(session, "grammar.lookup_valency", {"lemma": _canonical_key(lemma)})
                if item.get("frames") or item.get("tpas"):
                    valency.append(item)
            if not analyses and not valency:
                return None
            return {
                "source": "internal_read_only_grammar_mcp",
                "purpose": "morphology_and_valency_evidence_not_contextual_truth",
                "tokens": analyses,
                "valency": valency,
                "contextual_l2": contextual,
            }


__all__ = [
    "GrammarEvidenceClient",
    "TOOLS",
    "contextual_l2_checks",
    "_canonical_key",
]
