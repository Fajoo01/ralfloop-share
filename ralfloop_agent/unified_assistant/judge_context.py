from __future__ import annotations

from dataclasses import dataclass
import os
import re
from typing import Any, Callable

from src.mcp_transport import MCPClientSession, UnixMCPTransport

from .capability_rag_router import CapabilityRAGIndex


DEFAULT_MEMORY_SOCKET = "/run/ralf-memory-mcp/mcp.sock"
_MAX_CANDIDATES = 1
_MAX_DOCUMENTS = 1
_MAX_ENTITIES = 1
_MAX_SNIPPET = 72


@dataclass(frozen=True)
class JudgeContext:
    facts: tuple[str, ...] = ()
    evidence_refs: tuple[str, ...] = ()
    metadata: dict[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "facts": list(self.facts),
            "evidence_refs": list(self.evidence_refs),
            "metadata": dict(self.metadata or {}),
        }


def _default_memory_session() -> MCPClientSession:
    socket_path = os.getenv("RALF_MEMORY_MCP_SOCKET", DEFAULT_MEMORY_SOCKET)
    return MCPClientSession(
        UnixMCPTransport(socket_path, connect_timeout=0.5),
        timeout=2.0,
        client_name="bottazzi-motor-judge-context",
    )


def _compact_text(value: str, limit: int = _MAX_SNIPPET) -> str:
    return " ".join(str(value).split())[:limit]


def _memory_backed(candidate: dict[str, Any]) -> bool:
    return any(
        "memory.operational.mcp" in str(provider)
        for provider in candidate.get("providers") or ()
    )


def _query_attempts(goal: str, domain: str) -> tuple[str, ...]:
    generic = {
        "fix", "bug", "problema", "issue", "sistemare", "controlla",
        "controllare", "verifica", "verificare", "dobbiamo", "questa", "questo",
        "concreto", "test", "tests", "patch",
    }
    domain_key = domain.casefold()
    words = [
        token for token in re.findall(r"[\wÀ-ÿ-]+", goal, flags=re.UNICODE)
        if len(token) >= 4
        and token.casefold() not in generic
        and token.casefold() != domain_key
    ]
    attempts = list(words[:4])
    if domain and domain not in {"knowledge", "documents"}:
        attempts.append(domain)
    return tuple(dict.fromkeys(attempts))[:5]


def _short_hash(value: object) -> str:
    return str(value or "")[:8]


def _source_ref(source: dict[str, Any]) -> str:
    system = str(source.get("system") or "unknown")
    native_id = str(source.get("native_id") or "unknown")
    content_hash = _short_hash(source.get("content_hash"))
    suffix = f"#{content_hash}" if content_hash else ""
    return f"{system}:{native_id}{suffix}"


def _document_record(row: dict[str, Any]) -> tuple[dict[str, Any], tuple[str, ...]]:
    source = dict(row.get("source") or {})
    document_id = str(row.get("document_id") or "unknown")
    content_hash = str(row.get("content_hash") or "")
    record = {
        "kind": "doc",
        "id": document_id,
        "title": _compact_text(str(row.get("title") or ""), 72),
        "hash": _short_hash(content_hash),
        "source": f"{source.get('system','')}:{source.get('native_id','')}",
        "snippet_untrusted": _compact_text(str(row.get("body") or "")),
    }
    refs = [f"doc:{document_id}#{_short_hash(content_hash)}"]
    if source:
        refs.append(_source_ref(source))
    return record, tuple(refs)


def _entity_record(row: dict[str, Any]) -> tuple[dict[str, Any], tuple[str, ...]]:
    entity_id = str(row.get("entity_id") or "unknown")
    content_hash = str(row.get("content_hash") or "")
    provenance = list(row.get("provenance") or ())[:4]
    record = {
        "kind": "entity",
        "id": entity_id,
        "type": str(row.get("entity_type") or ""),
        "status": str(row.get("status") or ""),
        "hash": _short_hash(content_hash),
    }
    refs = [f"entity:{entity_id}#{_short_hash(content_hash)}"]
    first_source = next((source for source in provenance if isinstance(source, dict)), None)
    if first_source is not None:
        refs.append(_source_ref(dict(first_source)))
    return record, tuple(refs)


def _collect_memory(
    goal: str,
    domain: str,
    session_factory: Callable[[], MCPClientSession],
) -> tuple[list[dict[str, Any]], list[str], str]:
    evidence: list[dict[str, Any]] = []
    refs: list[str] = []
    seen_docs: set[str] = set()
    try:
        with session_factory() as client:
            names = {tool.name for tool in client.list_tools()}
            if "memory_search_documents" in names:
                for query in _query_attempts(goal, domain):
                    result = client.call_tool(
                        "memory_search_documents", {"query": query, "limit": _MAX_DOCUMENTS}
                    )
                    rows = (result.get("structuredContent") or {}).get("result") or []
                    for row in rows:
                        if not isinstance(row, dict):
                            continue
                        document_id = str(row.get("document_id") or "")
                        if not document_id or document_id in seen_docs:
                            continue
                        record, row_refs = _document_record(row)
                        evidence.append(record)
                        refs.extend(row_refs)
                        seen_docs.add(document_id)
                        if len(seen_docs) >= _MAX_DOCUMENTS:
                            break
                    if len(seen_docs) >= _MAX_DOCUMENTS:
                        break
            if not evidence and domain not in {"", "knowledge", "documents"} and "memory_list_entities" in names:
                result = client.call_tool(
                    "memory_list_entities", {"domain": domain, "limit": _MAX_ENTITIES}
                )
                rows = (result.get("structuredContent") or {}).get("result") or []
                for row in rows[:_MAX_ENTITIES]:
                    if not isinstance(row, dict):
                        continue
                    record, row_refs = _entity_record(row)
                    evidence.append(record)
                    refs.extend(row_refs)
    except Exception as exc:
        return [], [], f"unavailable:{type(exc).__name__}"
    return evidence, list(dict.fromkeys(refs))[:2], "available"


def collect_judge_context(
    user_goal: str,
    *,
    index: CapabilityRAGIndex | None = None,
    memory_session_factory: Callable[[], MCPClientSession] | None = None,
) -> JudgeContext:
    capability_index = index or CapabilityRAGIndex()
    candidates = tuple(capability_index.discover(user_goal, limit=_MAX_CANDIDATES))
    raw_rows = [candidate.as_dict() for candidate in candidates]
    memory_candidate = next((row for row in raw_rows if _memory_backed(row)), None)
    candidate_rows = []
    for row in raw_rows:
        providers = list(row.get("providers") or ())
        provider = str(providers[0]).split("=>", 1)[-1].split("[", 1)[0] if providers else "NONE"
        candidate_rows.append({
            "skill": row["skill"], "domain": row["domain"],
            "policy": row["policy"], "provider": provider,
            "status": row["status"],
        })
    facts: list[str] = []
    evidence: list[dict[str, Any]] = []
    refs: list[str] = []
    memory_status = "not_needed"
    if memory_candidate is not None:
        evidence, refs, memory_status = _collect_memory(
            user_goal,
            str(memory_candidate.get("domain") or ""),
            memory_session_factory or _default_memory_session,
        )
    if candidate_rows:
        row = candidate_rows[0]
        facts.append(
            "retrieval="
            + f"{row['skill']}|{row['domain']}|{row['provider']}|{memory_status}|{len(evidence)}"
        )
    metadata = {
        "capability": candidate_rows[0] if candidate_rows else None,
        "memory": {"status": memory_status, "evidence_untrusted": evidence},
    }
    return JudgeContext(
        facts=tuple(facts),
        evidence_refs=tuple(refs),
        metadata=metadata,
    )


__all__ = ["DEFAULT_MEMORY_SOCKET", "JudgeContext", "collect_judge_context"]
