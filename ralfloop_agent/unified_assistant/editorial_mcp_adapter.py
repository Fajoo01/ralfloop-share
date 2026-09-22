"""Strict MCP adapter and deterministic dispatcher for local editorial flyers."""
from __future__ import annotations

import json
import os
import re
from typing import Any, Mapping

from src.mcp_transport import MCPClientSession, MCPProtocolError, UnixMCPTransport

EDITORIAL_TOOLS = frozenset({
    "flyer_create", "flyer_update", "flyer_projects", "flyer_brief",
    "flyer_review", "flyer_marketing_review", "flyer_media_review", "flyer_render",
})
_GENERIC = {
    "volantino", "volantini", "flyer", "locandina", "locandine", "manifesto", "manifesti",
    "poster", "tiremm", "innanz", "aps", "il", "la", "lo", "i", "le", "un", "una", "di",
    "del", "della", "dei", "delle", "per", "e", "con", "su", "questo", "questa",
    "valuta", "valutare", "controlla", "controllare", "rivedi", "review", "giudica",
    "genera", "render", "renderizza", "esporta", "pdf", "marketing", "grafica",
}


def _payload(result: Mapping[str, Any]) -> Any:
    if result.get("isError"):
        raise MCPProtocolError("editorial_tool_error")
    structured = result.get("structuredContent")
    if structured is not None:
        if isinstance(structured, dict) and set(structured) == {"result"}:
            return structured["result"]
        return structured
    content = result.get("content") or ()
    if content and isinstance(content[0], Mapping):
        text = content[0].get("text")
        if isinstance(text, str):
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                return text
    return result


def _tokens(text: str) -> set[str]:
    synonyms = {
        "ciclofficina": "ciclomeccanica", "bici": "ciclomeccanica",
        "bicicletta": "ciclomeccanica", "biciclette": "ciclomeccanica",
        "tessera": "tesseramento", "tessere": "tesseramento",
    }
    tokens = set()
    for token in re.findall(r"[a-z0-9à-ÿ]+", text.casefold()):
        if len(token) > 2 and token not in _GENERIC:
            tokens.add(synonyms.get(token, token))
    return tokens


class EditorialMCPContext:
    def __init__(self, socket_path: str = "/tmp/ralf-editorial-mcp/mcp.sock", timeout: float = 30):
        self.socket_path = socket_path
        self.timeout = timeout
        self.session = None

    @classmethod
    def from_environment(cls):
        return cls(os.getenv("RALF_EDITORIAL_MCP_SOCKET", "/tmp/ralf-editorial-mcp/mcp.sock"))

    def __enter__(self):
        self.session = MCPClientSession(
            UnixMCPTransport(self.socket_path), timeout=self.timeout, client_name="bot-tazzi-editorial"
        )
        self.session.__enter__()
        found = {tool.name for tool in self.session.list_tools()}
        if found != EDITORIAL_TOOLS:
            self.session.close()
            self.session = None
            raise MCPProtocolError("editorial_tool_allowlist_mismatch")
        return self

    def __exit__(self, *args):
        if self.session is not None:
            self.session.__exit__(*args)
            self.session = None

    def call(self, name: str, arguments: Mapping[str, Any]) -> Mapping[str, Any]:
        if name not in EDITORIAL_TOOLS or self.session is None:
            raise MCPProtocolError("editorial_tool_not_available")
        return self.session.call_tool(name, arguments)

    def payload(self, name: str, arguments: Mapping[str, Any]) -> Any:
        return _payload(self.call(name, arguments))

    def _resolve_project(self, objective: str) -> tuple[str | None, dict[str, Any] | None, list[str]]:
        raw = self.payload("flyer_projects", {})
        projects = list(raw if isinstance(raw, list) else raw.get("projects", []) if isinstance(raw, dict) else [])
        if not projects:
            return None, None, []
        folded = objective.casefold()
        for project in projects:
            if str(project).casefold() in folded:
                brief = self.payload("flyer_brief", {"project": project})
                return str(project), brief if isinstance(brief, dict) else None, [str(x) for x in projects]

        raw_wanted = {token for token in re.findall(r"[a-z0-9à-ÿ]+", folded) if len(token) > 2}
        slug_matches: list[str] = []
        for project in projects:
            slug_tokens = {
                token for token in re.findall(r"[a-z0-9à-ÿ]+", str(project).casefold())
                if len(token) > 2 and token not in _GENERIC and not token.isdigit()
            }
            if raw_wanted & slug_tokens:
                slug_matches.append(str(project))
        if len(slug_matches) == 1:
            project = slug_matches[0]
            brief = self.payload("flyer_brief", {"project": project})
            return project, brief if isinstance(brief, dict) else None, [str(x) for x in projects]

        wanted = _tokens(objective)
        ranked: list[tuple[int, str, dict[str, Any] | None]] = []
        for project in projects:
            brief = self.payload("flyer_brief", {"project": project})
            searchable = str(project).replace("-", " ")
            if isinstance(brief, dict):
                searchable += " " + str(brief.get("title") or "")
                for section in brief.get("sections") or ():
                    if not isinstance(section, dict):
                        continue
                    searchable += " " + str(section.get("title") or "")
                    searchable += " " + str(section.get("text") or "")
                    for media_key in ("image", "badge"):
                        media = section.get(media_key)
                        if isinstance(media, dict):
                            searchable += " " + str(media.get("caption") or "")
                    searchable += " " + " ".join(str(x) for x in section.get("sources") or ())
            score = len(wanted & _tokens(searchable))
            ranked.append((score, str(project), brief if isinstance(brief, dict) else None))
        ranked.sort(key=lambda row: (row[0], row[1]), reverse=True)
        if ranked and ranked[0][0] > 0 and (len(ranked) == 1 or ranked[0][0] > ranked[1][0]):
            return ranked[0][1], ranked[0][2], [str(x) for x in projects]
        return None, None, [str(x) for x in projects]

    def request(self, objective: str) -> dict[str, Any]:
        """Execute safe local editorial review/render requests from natural language."""
        lowered = objective.casefold()
        project, brief, projects = self._resolve_project(objective)
        list_only = bool(re.search(r"\b(?:elenca|lista|quali|progetti)\b", lowered))
        if list_only and project is None:
            return {
                "status": "completed", "projects": projects,
                "message": "Progetti editoriali: " + (", ".join(projects) if projects else "nessuno"),
            }
        if project is None:
            if re.search(r"\b(?:crea|prepara|fai|nuov[oa])\b", lowered):
                return {
                    "status": "clarification_required", "projects": projects,
                    "message": "Per creare un nuovo volantino serve un brief strutturato con contenuti, fonti e asset; non invento questi dati. Progetti esistenti: " + (", ".join(projects) if projects else "nessuno"),
                }
            return {
                "status": "clarification_required", "projects": projects,
                "message": "Non riesco a identificare un solo progetto editoriale. Specifica uno di: " + (", ".join(projects) if projects else "nessuno"),
            }
        args = {"project": project}
        if re.search(r"\b(?:render|renderizza|genera\s+(?:il\s+)?pdf|esporta|rigenera)\b", lowered):
            result = self.payload("flyer_render", args)
            ready = bool(result.get("print_ready")) if isinstance(result, dict) else False
            output = str(result.get("output") or "") if isinstance(result, dict) else ""
            return {
                "status": "completed" if ready else "draft",
                "project": project, "operation": "render", "result": result,
                "message": f"Render {project}: {'pronto per stampa' if ready else 'bozza/non pronto'}." + (f" Output: {output}" if output else ""),
                "evidence_refs": [output] if output else [],
            }
        if re.search(r"\b(?:brief|testi|contenuti)\b", lowered):
            return {"status": "completed", "project": project, "operation": "brief", "brief": brief,
                    "message": f"Brief di {project} recuperato e validato."}
        if re.search(r"\b(?:foto|immagini|media|dpi|crop|ritaglio|risoluzione)\b", lowered):
            result = self.payload("flyer_media_review", args)
            warnings = result.get("warnings") or [] if isinstance(result, dict) else []
            return {"status": "completed", "project": project, "operation": "media_review", "media": result,
                    "message": f"Revisione media {project}: {len(warnings)} avvisi."}
        editorial = self.payload("flyer_review", args)
        marketing = self.payload("flyer_marketing_review", args)
        media = self.payload("flyer_media_review", args)
        score = marketing.get("score") if isinstance(marketing, dict) else None
        grade = marketing.get("grade") if isinstance(marketing, dict) else None
        warnings = []
        for report in (editorial, marketing, media):
            if isinstance(report, dict):
                warnings.extend(str(x) for x in report.get("warnings") or ())
        return {
            "status": "completed", "project": project, "operation": "full_review",
            "editorial": editorial, "marketing": marketing, "media": media,
            "message": f"Revisione {project}: marketing {score}/100, classe {grade}; {len(warnings)} avvisi.",
        }
