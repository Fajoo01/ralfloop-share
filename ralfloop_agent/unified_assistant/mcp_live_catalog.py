from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import re
import unicodedata
from typing import Any, Callable

from src.mcp_transport import MCPClientSession, UnixMCPTransport


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CATALOG = PROJECT_ROOT / "config" / "mcp_catalog_v1.json"


def _normalize(value: str) -> str:
    value = unicodedata.normalize("NFKD", value.casefold())
    value = "".join(ch for ch in value if not unicodedata.combining(ch))
    return " ".join(value.split())


def _terms(value: str) -> frozenset[str]:
    return frozenset(
        token for token in re.findall(r"[a-z0-9]+", _normalize(value))
        if len(token) > 1
    )


@dataclass(frozen=True)
class LiveMCPTool:
    provider: str
    socket: str
    name: str
    description: str
    input_schema: dict[str, Any]
    score: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "socket": self.socket,
            "tool": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
            "score": round(self.score, 3),
        }


@dataclass(frozen=True)
class MCPProviderHealth:
    provider: str
    socket: str
    status: str
    tool_count: int = 0
    error: str | None = None


class LiveMCPCatalog:
    """Read-only discovery of MCP tool surfaces via initialize + tools/list only."""

    def __init__(
        self,
        path: str | Path = DEFAULT_CATALOG,
        *,
        session_factory: Callable[[str], MCPClientSession] | None = None,
    ) -> None:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        if payload.get("schema_version") != 1:
            raise ValueError("invalid_mcp_catalog")
        self.providers = tuple(
            (str(row["id"]), str(row["socket"]))
            for row in payload.get("providers", ())
        )
        self.session_factory = session_factory or self._session

    @staticmethod
    def _session(socket_path: str) -> MCPClientSession:
        return MCPClientSession(
            UnixMCPTransport(socket_path, connect_timeout=0.5),
            timeout=1.5,
            client_name="capability-rag-catalog",
        )

    def snapshot(
        self,
    ) -> tuple[tuple[LiveMCPTool, ...], tuple[MCPProviderHealth, ...]]:
        tools: list[LiveMCPTool] = []
        health: list[MCPProviderHealth] = []
        for provider, socket_path in self.providers:
            try:
                with self.session_factory(socket_path) as client:
                    rows = client.list_tools()
            except Exception as exc:
                health.append(MCPProviderHealth(
                    provider=provider, socket=socket_path, status="unavailable",
                    error=f"{type(exc).__name__}:{str(exc)[:160]}",
                ))
                continue
            for tool in rows:
                tools.append(LiveMCPTool(
                    provider=provider, socket=socket_path, name=tool.name,
                    description=tool.description,
                    input_schema=dict(tool.input_schema),
                ))
            health.append(MCPProviderHealth(
                provider=provider, socket=socket_path, status="available",
                tool_count=len(rows),
            ))
        return tuple(tools), tuple(health)

    def discover(self, query: str, *, limit: int = 20) -> tuple[LiveMCPTool, ...]:
        if not 1 <= limit <= 50:
            raise ValueError("mcp_catalog_limit_invalid")
        query_terms = _terms(query)
        normalized = _normalize(query)
        tools, _health = self.snapshot()
        ranked: list[LiveMCPTool] = []
        for tool in tools:
            document = " ".join((
                tool.provider,
                tool.name.replace("_", " ").replace(".", " "),
                tool.description,
                json.dumps(tool.input_schema, ensure_ascii=False, sort_keys=True),
            ))
            score = float(len(query_terms & _terms(document)))
            if _normalize(tool.provider) in normalized:
                score += 5.0
            tool_words = _normalize(tool.name.replace("_", " ").replace(".", " "))
            tool_terms = _terms(tool_words)
            if tool_words and tool_words in normalized:
                score += 6.0
            elif query_terms and query_terms <= tool_terms:
                score += 4.0
            if score < 2.0:
                continue
            ranked.append(LiveMCPTool(
                provider=tool.provider, socket=tool.socket, name=tool.name,
                description=tool.description, input_schema=tool.input_schema,
                score=score,
            ))
        ranked.sort(key=lambda row: (-row.score, row.provider, row.name))
        return tuple(ranked[:limit])


__all__ = ["DEFAULT_CATALOG", "LiveMCPCatalog", "LiveMCPTool", "MCPProviderHealth"]
