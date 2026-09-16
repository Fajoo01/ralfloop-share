"""Strict Unix-MCP adapter for Bot-tazzi funding discovery and review."""
from __future__ import annotations

import json
import os
import re
from typing import Any, Mapping

from src.mcp_transport import MCPClientSession, MCPProtocolError, UnixMCPTransport

_BANDO_REF_RE = re.compile(r"\bRL[A-Z]\d{8,16}\b", re.I)

BANDI_TOOLS = frozenset({
    "bandi_research_now",
    "bandi_latest",
    "bandi_search_latest",
    "bandi_get_opportunity",
})


def _payload(result: Mapping[str, Any]) -> dict[str, Any]:
    if result.get("isError"):
        structured = result.get("structuredContent")
        code = structured.get("status") if isinstance(structured, Mapping) else "bandi_tool_error"
        raise MCPProtocolError(str(code or "bandi_tool_error"))
    structured = result.get("structuredContent")
    if isinstance(structured, Mapping):
        return dict(structured)
    content = result.get("content") or ()
    if content and isinstance(content[0], Mapping) and isinstance(content[0].get("text"), str):
        decoded = json.loads(content[0]["text"])
        if isinstance(decoded, dict):
            return decoded
    raise MCPProtocolError("bandi_tool_malformed")


class BandiMCPContext:
    def __init__(self, socket_path: str = "/tmp/ralf-bandi-mcp/mcp.sock", timeout: float = 240):
        self.socket_path = socket_path
        self.timeout = timeout
        self.session: MCPClientSession | None = None

    @classmethod
    def from_environment(cls) -> "BandiMCPContext":
        return cls(os.getenv("RALF_BANDI_MCP_SOCKET", "/tmp/ralf-bandi-mcp/mcp.sock"))

    def __enter__(self) -> "BandiMCPContext":
        self.session = MCPClientSession(
            UnixMCPTransport(self.socket_path), timeout=self.timeout, client_name="bot-tazzi-bandi"
        )
        self.session.__enter__()
        found = {tool.name for tool in self.session.list_tools()}
        if found != BANDI_TOOLS:
            self.session.close()
            self.session = None
            raise MCPProtocolError("bandi_tool_allowlist_mismatch")
        return self

    def __exit__(self, *args: object) -> None:
        if self.session is not None:
            self.session.__exit__(*args)
            self.session = None

    def call(self, name: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
        if name not in BANDI_TOOLS or self.session is None:
            raise MCPProtocolError("bandi_tool_not_available")
        return _payload(self.session.call_tool(name, arguments))

    def request(self, objective: str) -> dict[str, Any]:
        lowered = objective.casefold()
        refs = tuple(dict.fromkeys(match.upper() for match in _BANDO_REF_RE.findall(objective)))
        fresh = bool(re.search(r"\b(?:cerca|trova|scopri|ricerca|aggiorna|nuov[ioe]|adesso|oggi|ora)\b", lowered))
        review = bool(re.search(r"\b(?:valuta|ammissibil|compatibil|conviene|requisit|scaden|analizz|verifica)\b", lowered))
        list_only = bool(re.search(r"\b(?:lista|elenca|quali|aperti|opportunit[aà])\b", lowered))

        if refs:
            raw = self.call("bandi_search_latest", {"query": " ".join(refs), "limit": 5})
            if not raw.get("items"):
                raw = self.call("bandi_research_now", {"focus": objective[:500], "limit": 10})
        elif fresh:
            raw = self.call("bandi_research_now", {"focus": objective[:500], "limit": 10})
        elif review:
            raw = self.call("bandi_search_latest", {"query": objective[:500], "limit": 5})
            if not raw.get("items"):
                raw = self.call("bandi_research_now", {"focus": objective[:500], "limit": 10})
        elif list_only:
            raw = self.call("bandi_latest", {"limit": 12})
            if not raw.get("items"):
                raw = self.call("bandi_research_now", {"focus": objective[:500], "limit": 10})
        else:
            raw = self.call("bandi_latest", {"limit": 10})
            if not raw.get("items"):
                raw = self.call("bandi_research_now", {"focus": objective[:500], "limit": 10})

        items = list(raw.get("items") or [])
        if review and len(items) == 1 and items[0].get("call_key"):
            detail = self.call("bandi_get_opportunity", {"call_key": str(items[0]["call_key"])})
            raw["selected_opportunity"] = detail.get("opportunity")

        raw["message"] = _message(raw)
        raw["evidence_refs"] = _evidence_refs(raw)
        raw["requested_bando_refs"] = list(refs)
        raw["operation"] = "reference_lookup" if refs else "fresh_research" if fresh else "review" if review else "latest"
        return raw


def _message(payload: Mapping[str, Any]) -> str:
    items = list(payload.get("items") or [])
    status = str(payload.get("status") or "completed")
    if not items:
        if status == "no_report":
            return "Non c'è ancora un report bandi persistito; la ricerca fresca non ha prodotto un report utilizzabile."
        return "Nessun bando compatibile trovato nelle fonti verificate per questa ricerca."
    lines = [f"Bandi: {len(items)} opportunità rilevanti ({status})."]
    for item in items[:5]:
        title = str(item.get("title") or "senza titolo")
        issuer = str(item.get("issuer") or "ente non indicato")
        deadline = str(item.get("deadline") or "scadenza da verificare")
        score = item.get("score")
        priority = str(item.get("priority") or "")
        lines.append(f"- {title} — {issuer}; scadenza {deadline}; punteggio {score if score is not None else '?'} {priority}".rstrip())
    return "\n".join(lines)


def _evidence_refs(payload: Mapping[str, Any]) -> list[str]:
    refs: list[str] = []
    report = str(payload.get("report_json") or "")
    if report:
        refs.append(report)
    for item in payload.get("items") or ():
        if isinstance(item, Mapping):
            url = str(item.get("primary_url") or "")
            if url and url not in refs:
                refs.append(url)
    return refs[:64]


__all__ = ["BANDI_TOOLS", "BandiMCPContext"]
