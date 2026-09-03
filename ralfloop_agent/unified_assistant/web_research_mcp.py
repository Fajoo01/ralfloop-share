from __future__ import annotations

import json
from typing import Any, Mapping

from .web_research_service import WebResearchService


TOOLS = {
    "research_search_web", "research_find_authoritative_source", "research_open_source",
    "research_compare_sources", "research_extract_evidence", "research_verify_claim",
}


class WebResearchMCPServer:
    def __init__(self, service: WebResearchService) -> None:
        self.service = service

    def list_tools(self) -> list[dict[str, Any]]:
        schemas = {
            "research_search_web": ({"query": _text(500), "limit": _limit()}, ["query"]),
            "research_find_authoritative_source": ({"query": _text(500), "limit": _limit()}, ["query"]),
            "research_open_source": ({"source_id": _source_id()}, ["source_id"]),
            "research_compare_sources": ({"source_ids": _source_ids(2), "terms": _terms()}, ["source_ids", "terms"]),
            "research_extract_evidence": ({"source_id": _source_id(), "terms": _terms()}, ["source_id", "terms"]),
            "research_verify_claim": ({"claim": _text(2000), "source_ids": _source_ids(1)}, ["claim", "source_ids"]),
        }
        return [{"name": name, "description": f"Source-backed semantic research capability: {name}.", "inputSchema": {
            "type": "object", "properties": schemas[name][0], "required": schemas[name][1], "additionalProperties": False,
        }} for name in sorted(TOOLS)]

    def call(self, name: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
        if name not in TOOLS or not isinstance(arguments, Mapping):
            return _error("POLICY_DENIED")
        allowed = {
            "research_search_web": ({"query", "limit"}, {"query"}),
            "research_find_authoritative_source": ({"query", "limit"}, {"query"}),
            "research_open_source": ({"source_id"}, {"source_id"}),
            "research_compare_sources": ({"source_ids", "terms"}, {"source_ids", "terms"}),
            "research_extract_evidence": ({"source_id", "terms"}, {"source_id", "terms"}),
            "research_verify_claim": ({"claim", "source_ids"}, {"claim", "source_ids"}),
        }
        permitted, required = allowed[name]
        if not set(arguments) <= permitted or not required <= set(arguments):
            return _error("POLICY_DENIED")
        try:
            if name == "research_search_web":
                data = self.service.search_web(str(arguments["query"]), limit=int(arguments.get("limit", 10)))
            elif name == "research_find_authoritative_source":
                data = self.service.find_authoritative_source(str(arguments["query"]), limit=int(arguments.get("limit", 10)))
            elif name == "research_open_source":
                data = self.service.open_source(str(arguments["source_id"]))
            elif name == "research_extract_evidence":
                data = self.service.extract_evidence(str(arguments["source_id"]), tuple(arguments["terms"]))
            elif name == "research_compare_sources":
                data = self.service.compare_sources(tuple(arguments["source_ids"]), tuple(arguments["terms"]))
            else:
                data = self.service.verify_claim(str(arguments["claim"]), tuple(arguments["source_ids"]))
            payload = data.model_dump(mode="json") if hasattr(data, "model_dump") else [row.model_dump(mode="json") for row in data]
            return _result(payload)
        except KeyError:
            return _error("POLICY_DENIED")
        except ValueError as exc:
            code = "NOT_FOUND" if "not_discovered" in str(exc) else "MALFORMED_RESPONSE"
            return _error(code)
        except Exception:
            return _error("SOURCE_UNAVAILABLE")


def _text(maximum): return {"type": "string", "minLength": 1, "maxLength": maximum}
def _limit(): return {"type": "integer", "minimum": 1, "maximum": 30}
def _source_id(): return {"type": "string", "pattern": r"^research\.[a-f0-9]{24}$"}
def _source_ids(minimum): return {"type": "array", "items": _source_id(), "minItems": minimum, "maxItems": 10, "uniqueItems": True}
def _terms(): return {"type": "array", "items": _text(200), "minItems": 1, "maxItems": 20}


def _result(data):
    payload = {"ok": True, "data": data, "writes": 0, "sends": 0}
    return {"content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False)}], "structuredContent": payload, "isError": False}


def _error(code):
    payload = {"ok": False, "status": code, "writes": 0, "sends": 0}
    return {"content": [{"type": "text", "text": code}], "structuredContent": payload, "isError": True}


__all__ = ["TOOLS", "WebResearchMCPServer"]
