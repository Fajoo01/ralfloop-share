from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
import json
from pathlib import Path
import re
import time
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
        self.provider_globs = tuple(str(value) for value in payload.get("provider_globs", ()))
        self.term_aliases = {
            _normalize(str(key)): tuple(str(value) for value in values)
            for key, values in dict(payload.get("term_aliases", {})).items()
        }
        self.session_factory = session_factory or self._session
        self.cache_ttl = float(payload.get("cache_ttl_seconds", 15.0))
        self.max_workers = max(1, min(int(payload.get("max_workers", 8)), 16))
        self._snapshot_cache = None
        self._snapshot_cached_at = 0.0
        self._snapshot_provider_signature = ()

    @staticmethod
    def _session(socket_path: str) -> MCPClientSession:
        return MCPClientSession(
            UnixMCPTransport(socket_path, connect_timeout=0.5),
            timeout=1.5,
            client_name="capability-rag-catalog",
        )

    @staticmethod
    def _derived_provider_id(socket_path: str) -> str:
        parent = Path(socket_path).parent.name
        if parent.startswith("ralf-") and parent.endswith("-mcp"):
            parent = parent[5:-4]
        return re.sub(r"[^a-z0-9]+", "_", parent.casefold()).strip("_") or "mcp"

    def _provider_rows(self) -> tuple[tuple[str, str], ...]:
        by_provider = {provider: socket for provider, socket in self.providers}
        for pattern in self.provider_globs:
            for path in sorted(Path("/").glob(pattern.lstrip("/"))):
                if not path.is_socket():
                    continue
                provider = self._derived_provider_id(str(path))
                current = by_provider.get(provider)
                if current is None or not Path(current).is_socket():
                    by_provider[provider] = str(path)
        return tuple(sorted(by_provider.items()))

    def _scan_provider(self, row: tuple[str, str]):
        provider, socket_path = row
        try:
            with self.session_factory(socket_path) as client:
                rows = client.list_tools()
        except Exception as exc:
            return (), MCPProviderHealth(
                provider=provider, socket=socket_path, status="unavailable",
                error=f"{type(exc).__name__}:{str(exc)[:160]}",
            )
        tools = tuple(LiveMCPTool(
            provider=provider, socket=socket_path, name=tool.name,
            description=tool.description, input_schema=dict(tool.input_schema),
        ) for tool in rows)
        return tools, MCPProviderHealth(
            provider=provider, socket=socket_path, status="available", tool_count=len(rows),
        )

    def snapshot(
        self, *, force: bool = False,
    ) -> tuple[tuple[LiveMCPTool, ...], tuple[MCPProviderHealth, ...]]:
        provider_rows = self._provider_rows()
        now = time.monotonic()
        if (
            not force and self._snapshot_cache is not None
            and provider_rows == self._snapshot_provider_signature
            and now - self._snapshot_cached_at < self.cache_ttl
        ):
            return self._snapshot_cache
        tools: list[LiveMCPTool] = []
        health: list[MCPProviderHealth] = []
        workers = min(self.max_workers, len(provider_rows)) if provider_rows else 1
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="mcp-catalog") as pool:
            scanned = pool.map(self._scan_provider, provider_rows)
            for provider_tools, provider_health in scanned:
                tools.extend(provider_tools)
                health.append(provider_health)
        result = (tuple(tools), tuple(health))
        self._snapshot_cache = result
        self._snapshot_cached_at = now
        self._snapshot_provider_signature = provider_rows
        return result

    def discover(self, query: str, *, limit: int = 20) -> tuple[LiveMCPTool, ...]:
        if not 1 <= limit <= 50:
            raise ValueError("mcp_catalog_limit_invalid")
        base_terms = _terms(query)
        expanded_terms = set(base_terms)
        for term in base_terms:
            for alias in self.term_aliases.get(term, ()):
                expanded_terms.update(_terms(alias))
        query_terms = frozenset(expanded_terms)
        normalized = _normalize(query)
        tools, _health = self.snapshot()
        ranked: list[LiveMCPTool] = []
        for tool in tools:
            tool_words = _normalize(tool.name.replace("_", " ").replace(".", " "))
            tool_terms = _terms(tool_words)
            description_terms = _terms(tool.description)
            schema_terms = _terms(json.dumps(tool.input_schema, ensure_ascii=False, sort_keys=True))
            score = float(3 * len(query_terms & tool_terms))
            score += float(1.5 * len(query_terms & description_terms))
            score += float(0.25 * len(query_terms & schema_terms))
            if _normalize(tool.provider) in normalized:
                score += 5.0
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
