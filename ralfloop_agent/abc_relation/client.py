from __future__ import annotations

from typing import Any, Mapping

from src.mcp_transport import MCPClientSession, UnixMCPTransport


DEFAULT_SOCKET = "/run/ralf-abc-relation-mcp/mcp.sock"


class RelationMCPClient:
    def __init__(self, socket_path: str = DEFAULT_SOCKET, *, timeout: float = 10.0) -> None:
        self.socket_path = socket_path
        self.timeout = timeout

    def call(self, name: str, arguments: Mapping[str, Any] | None = None) -> Any:
        transport = UnixMCPTransport(self.socket_path, connect_timeout=min(3.0, self.timeout))
        with MCPClientSession(transport, timeout=self.timeout, client_name="ralf-abc-relation-client") as session:
            session.list_tools()
            response = session.call_tool(name, dict(arguments or {}))
        structured = response.get("structuredContent")
        if not isinstance(structured, Mapping) or structured.get("ok") is not True:
            raise RuntimeError("abc_relation_mcp_malformed_response")
        return structured.get("result")

    def get_state(self) -> Mapping[str, Any]:
        value = self.call("abc_get_state")
        if not isinstance(value, Mapping):
            raise RuntimeError("abc_relation_state_malformed")
        return value

    def analyze(self) -> Mapping[str, Any]:
        value = self.call("abc_analyze")
        if not isinstance(value, Mapping):
            raise RuntimeError("abc_relation_analysis_malformed")
        return value

    def timeline(self, *, limit: int = 100, kind: str | None = None) -> list[Mapping[str, Any]]:
        args: dict[str, Any] = {"limit": limit}
        if kind:
            args["kind"] = kind
        value = self.call("abc_get_timeline", args)
        if not isinstance(value, Mapping) or not isinstance(value.get("events"), list):
            raise RuntimeError("abc_relation_timeline_malformed")
        return value["events"]

    def search(self, query: str, *, limit: int = 50) -> list[Mapping[str, Any]]:
        value = self.call("abc_search_events", {"query": query, "limit": limit})
        if not isinstance(value, Mapping) or not isinstance(value.get("events"), list):
            raise RuntimeError("abc_relation_search_malformed")
        return value["events"]

    def references(self) -> list[Mapping[str, Any]]:
        value = self.call("abc_get_reference_library")
        if not isinstance(value, Mapping) or not isinstance(value.get("references"), list):
            raise RuntimeError("abc_relation_references_malformed")
        return value["references"]

    def propose(self, text: str, **kwargs: Any) -> Mapping[str, Any]:
        args = {"text": text, **{key: value for key, value in kwargs.items() if value is not None}}
        value = self.call("abc_propose_event", args)
        if not isinstance(value, Mapping) or not isinstance(value.get("events"), list):
            raise RuntimeError("abc_relation_proposal_malformed")
        if not isinstance(value.get("proposal_digest"), str):
            raise RuntimeError("abc_relation_proposal_malformed")
        return value


__all__ = ["DEFAULT_SOCKET", "RelationMCPClient"]
