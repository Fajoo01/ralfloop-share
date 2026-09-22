from __future__ import annotations

import re
import unicodedata
from typing import Any

from src.mcp_transport import MCPClientSession, UnixMCPTransport


TOOLS = {
    "grammar.lookup_token",
    "grammar.lookup_valency",
    "grammar.health",
}
TOKEN_RE = re.compile(r"[^\W\d_][\w'’-]{0,63}", re.UNICODE)


def _canonical_key(value: str) -> str:
    """NFC + Unicode casefold; preserve diacritics because Italian accents are contrastive."""
    return unicodedata.normalize("NFC", str(value or "")).casefold().replace("’", "'")


class GrammarEvidenceClient:
    """Bounded read-only client for the internal C grammar MCP."""

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
        normalized_text = unicodedata.normalize("NFC", text[:1200]).replace("’", "'")
        unique: list[str] = []
        seen: set[str] = set()

        def add(value: str) -> None:
            key = _canonical_key(value)
            if key and key not in seen and len(unique) < limit:
                unique.append(key)
                seen.add(key)

        for token in TOKEN_RE.findall(normalized_text):
            token = token.replace("’", "'")
            # Morph-it stores elided prefixes (l', dell', un') separately
            # from the following word. Keep trailing-apostrophe words such as po'.
            if "'" in token and not token.endswith("'"):
                prefix, suffix = token.split("'", 1)
                if prefix and suffix:
                    add(prefix + "'")
                    add(suffix)
                else:
                    add(token)
            else:
                add(token)
            if len(unique) >= limit:
                break
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
            verb_lemmas: list[str] = []
            for token in tokens:
                item = self._call(session, "grammar.lookup_token", {"token": _canonical_key(token)})
                if not item.get("analyses"):
                    continue
                analyses.append(item)
                for analysis in item["analyses"]:
                    if analysis.get("category") != "verbo":
                        continue
                    lemma = str((analysis.get("features") or {}).get("lemma") or "").strip()
                    if lemma and lemma not in verb_lemmas:
                        verb_lemmas.append(lemma)
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
            }


__all__ = ["GrammarEvidenceClient", "TOOLS"]
