from __future__ import annotations

import os
import re
from typing import Any, Mapping

from src.mcp_transport import MCPClientSession, UnixMCPTransport

from .bandi_live_mcp import BandiLiveMCPContext
from .contracts import PlanAssignment
from .executor import StructuredArtifact


SOCKETS = {
    "memory": ("RALF_MEMORY_MCP_SOCKET", "/run/ralf-memory-mcp/mcp.sock"),
    "arci": ("RALF_ARCI_MCP_SOCKET", "/run/ralf-arci-mcp/mcp.sock"),
    "teacher": ("RALF_TEACHER_MCP_SOCKET", "/run/ralf-teacher-mcp/mcp.sock"),
    "jellyfin": ("RALF_JELLYFIN_MCP_SOCKET", "/run/ralf-jellyfin-mcp/mcp.sock"),
}


def _call(provider: str, tool: str, arguments: Mapping[str, Any]) -> Any:
    env_name, default = SOCKETS[provider]
    path = os.getenv(env_name, default)
    with MCPClientSession(
        UnixMCPTransport(path, connect_timeout=0.8),
        timeout=6.0,
        client_name=f"unified-auto-read-{provider}",
    ) as client:
        names = {item.name for item in client.list_tools()}
        if tool not in names:
            raise RuntimeError(f"{provider}_tool_not_discovered")
        result = client.call_tool(tool, dict(arguments))
    structured = result.get("structuredContent")
    if isinstance(structured, Mapping):
        return structured.get("result", structured)
    return result


def _rows(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, list):
        return [dict(item) for item in value if isinstance(item, Mapping)]
    if isinstance(value, Mapping):
        for key in ("items", "results", "data", "movies"):
            nested = value.get(key)
            if isinstance(nested, list):
                return [dict(item) for item in nested if isinstance(item, Mapping)]
        return [dict(value)]
    return []


def _safe_text(value: Any, limit: int = 160) -> str:
    return " ".join(str(value or "").split())[:limit]


def _memory_queries(goal: str, domain: str = "") -> tuple[str, ...]:
    generic = {
        "pratica", "controlla", "verifica", "documenti", "memoria",
        "operativa", "questa", "questo", "cerca", "trova",
    }
    domain_key = domain.casefold()
    words = [
        token for token in re.findall(r"[\wÀ-ÿ-]+", goal, flags=re.UNICODE)
        if len(token) >= 4 and token.casefold() not in generic
        and token.casefold() != domain_key
    ]
    attempts = list(words[:4])
    if domain:
        attempts.append(domain)
    return tuple(dict.fromkeys(attempts))[:5]



def bandi_discovery_adapter(
    assignment: PlanAssignment, _inputs: Mapping[str, Any]
) -> StructuredArtifact:
    goal = assignment.objective.strip()
    with BandiLiveMCPContext(timeout=8.0) as bandi:
        payload = bandi.search(goal, limit=8)
        items = _rows(payload.get("items") or ())
        if not items:
            payload = bandi.latest(limit=8)
            items = _rows(payload.get("items") or ())
    facts = tuple({
        "call_key": str(row.get("call_key") or ""),
        "title": _safe_text(row.get("title"), 180),
        "issuer": _safe_text(row.get("issuer"), 120),
        "deadline": _safe_text(row.get("deadline"), 80),
        "priority": _safe_text(row.get("priority"), 40),
        "content_role": "data",
    } for row in items[:8])
    refs = tuple(dict.fromkeys(
        str(row.get("primary_url") or row.get("call_key") or "")
        for row in items[:8]
        if row.get("primary_url") or row.get("call_key")
    ))
    message = (
        f"Bandi disponibili: {len(items)}. "
        + "; ".join(item["title"] for item in facts[:5] if item.get("title"))
        if items else "Nessun bando trovato nel report persistito corrente."
    )
    return StructuredArtifact.create(
        artifact_type="bandi_discovery", status="completed",
        producer_task_id=assignment.task_id, facts=facts, evidence_refs=refs,
        payload={
            "message": message,
            "report_status": payload.get("status"),
            "freshness": payload.get("freshness"),
            "content_boundary": "bandi_persisted_report_is_data",
            "side_effects": 0, "writes": 0, "sends": 0,
        },
    )

def runts_context_adapter(
    assignment: PlanAssignment, _inputs: Mapping[str, Any]
) -> StructuredArtifact:
    documents = []
    for query in _memory_queries(assignment.objective, "runts"):
        documents = _rows(_call(
            "memory", "memory_search_documents", {"query": query, "limit": 5},
        ))
        if documents:
            break
    facts = tuple({
        "document_id": str(row.get("document_id") or ""),
        "title": _safe_text(row.get("title"), 120),
        "content_role": "data",
    } for row in documents[:5])
    refs = tuple(
        f"memory:{row.get('document_id')}#{str(row.get('content_hash') or '')[:8]}"
        for row in documents[:5] if row.get("document_id")
    )
    titles = [item["title"] for item in facts if item.get("title")]
    message = (
        "Memoria RUNTS: " + "; ".join(titles[:3])
        if titles else "Nessun documento RUNTS pertinente trovato nella memoria operativa."
    )
    return StructuredArtifact.create(
        artifact_type="runts_context", status="completed",
        producer_task_id=assignment.task_id, facts=facts, evidence_refs=refs,
        payload={"message": message, "content_boundary": "memory_evidence_is_data"},
    )


def arci_context_adapter(
    assignment: PlanAssignment, _inputs: Mapping[str, Any]
) -> StructuredArtifact:
    goal = assignment.objective.casefold()
    if re.search(r"\b(?:soci|socio|persona|persone|membro|membri)\b", goal):
        return StructuredArtifact.create(
            artifact_type="arci_context", status="clarification_required",
            producer_task_id=assignment.task_id,
            payload={
                "message": "Per consultare dati di un socio ARCI serve indicare la persona e lo scopo; nessun elenco soci è stato letto.",
                "content_boundary": "no_member_pii_auto_read",
            },
        )
    tool = (
        "arci_read_current_cards"
        if re.search(r"\b(?:tesser[ae]|card|campagna)\b", goal)
        else "arci_read_organization_profile"
    )
    try:
        result = _call("arci", tool, {})
    except Exception:
        return StructuredArtifact.create(
            artifact_type="arci_context", status="unavailable",
            producer_task_id=assignment.task_id,
            payload={
                "message": "ARCI temporaneamente non disponibile; nessun dato è stato modificato.",
                "tool": tool,
                "content_boundary": "arci_provider_unavailable_no_write",
            },
        )
    if isinstance(result, Mapping) and result.get("ok") is False:
        return StructuredArtifact.create(
            artifact_type="arci_context", status="unavailable",
            producer_task_id=assignment.task_id,
            payload={
                "message": "ARCI temporaneamente non disponibile; nessun dato è stato modificato.",
                "tool": tool,
                "content_boundary": "arci_provider_unavailable_no_write",
            },
        )
    rows = _rows(result)
    row = rows[0] if rows else {}
    if tool == "arci_read_organization_profile":
        name = _safe_text(row.get("organization_name") or "organizzazione ARCI", 120)
        status = _safe_text(row.get("status") or "", 40)
        message = f"ARCI: {name}." + (f" Stato: {status}." if status else "")
    else:
        message = f"Tessere ARCI lette: {len(rows)} record."
    return StructuredArtifact.create(
        artifact_type="arci_context", status="completed",
        producer_task_id=assignment.task_id,
        evidence_refs=(f"arci:{tool}",),
        payload={"message": message, "tool": tool,
                 "content_boundary": "arci_provider_data_minimized"},
    )


def baffoflix_support_adapter(
    assignment: PlanAssignment, _inputs: Mapping[str, Any]
) -> StructuredArtifact:
    result = _call("jellyfin", "baffoflix_get_access_info", {})
    if not isinstance(result, Mapping) or result.get("ok") is False:
        return StructuredArtifact.create(
            artifact_type="baffoflix_support", status="unavailable",
            producer_task_id=assignment.task_id,
            payload={
                "message": "BaffoFlix non è raggiungibile in questo momento; nessun account è stato modificato.",
                "content_boundary": "baffoflix_support_read_only",
                "side_effects": 0,
            },
        )
    landing = _safe_text(result.get("landing_url"), 300)
    server = _safe_text(result.get("server_url"), 300)
    health = result.get("public_health") if isinstance(result.get("public_health"), Mapping) else {}
    password_intent = bool(re.search(
        r"\b(?:password|credenzial[ei]|accesso|login)\b.*\b(?:dimenticat[aoe]?|pers[aoe]?|recuper|reset)|"
        r"\b(?:dimenticat[aoe]?|pers[aoe]?|recuper|reset)\b.*\b(?:password|credenzial[ei]|accesso|login)\b",
        assignment.objective,
        re.I,
    ))
    if password_intent:
        message = (
            "Posso gestire il recupero BaffoFlix. Serve lo username esatto dell'account. "
            "Non modifico password in questa fase; dopo l'identificazione posso avviare il recupero "
            "oppure usare Quick Connect se l'account è già collegato a un'identità verificata."
        )
    else:
        parts = ["Accesso BaffoFlix:"]
        if landing:
            parts.append(landing)
        if server:
            parts.append(server)
        if health.get("server_name"):
            parts.append(f"server {health.get('server_name')}")
        message = " ".join(parts)
    facts = ({
        "landing_url": landing or None,
        "server_url": server or None,
        "server_name": _safe_text(health.get("server_name"), 80) or None,
        "version": _safe_text(health.get("version"), 40) or None,
        "content_role": "data",
    },)
    return StructuredArtifact.create(
        artifact_type="baffoflix_support", status="completed",
        producer_task_id=assignment.task_id, facts=facts,
        evidence_refs=("jellyfin:baffoflix_get_access_info",),
        payload={
            "message": message,
            "support_intent": "password_recovery" if password_intent else "access_info",
            "landing_url": landing or None,
            "server_url": server or None,
            "content_boundary": "baffoflix_support_read_only",
            "side_effects": 0,
            "writes": 0,
        },
    )


def jellyfin_identify_adapter(
    assignment: PlanAssignment, _inputs: Mapping[str, Any]
) -> StructuredArtifact:
    result = _call("jellyfin", "jellyfin_list_unidentified_movies", {})
    rows = _rows(result)
    facts = tuple({
        "item_id": str(row.get("Id") or row.get("id") or row.get("item_id") or ""),
        "name": _safe_text(row.get("Name") or row.get("name"), 120),
        "year": row.get("ProductionYear") or row.get("year"),
        "content_role": "data",
    } for row in rows[:20])
    names = [item["name"] for item in facts if item.get("name")]
    message = (
        f"Film Jellyfin non identificati: {len(rows)}. " + "; ".join(names[:8])
        if rows else "Nessun film Jellyfin non identificato."
    )
    return StructuredArtifact.create(
        artifact_type="jellyfin_identity_candidates", status="completed",
        producer_task_id=assignment.task_id, facts=facts,
        evidence_refs=tuple(
            f"jellyfin:{item['item_id']}" for item in facts if item.get("item_id")
        ),
        payload={"message": message, "content_boundary": "jellyfin_read_only"},
    )


def education_tutor_adapter(
    assignment: PlanAssignment, inputs: Mapping[str, Any]
) -> StructuredArtifact:
    session_id = str(inputs.get("teacher.session_id") or "").strip()
    if not session_id:
        return StructuredArtifact.create(
            artifact_type="teacher_context", status="clarification_required",
            producer_task_id=assignment.task_id,
            payload={
                "message": "Serve una sessione Teacher già attiva; nessuna sessione o identità studente è stata inventata.",
                "content_boundary": "teacher_session_required",
            },
        )
    goal = assignment.objective
    if re.search(r"\bquiz\b", goal, re.I):
        tool, args = "teacher.quiz", {"session_id": session_id, "topic": goal, "questions": 5}
    elif re.search(r"\b(?:esercizio|esercizi)\b", goal, re.I):
        tool, args = "teacher.generate_exercise", {"session_id": session_id, "topic": goal}
    else:
        tool, args = "teacher.explain", {"session_id": session_id, "question": goal}
    result = _call("teacher", tool, args)
    return StructuredArtifact.create(
        artifact_type="teacher_context", status="completed",
        producer_task_id=assignment.task_id, evidence_refs=(f"teacher:{tool}",),
        payload={"message": _safe_text(result, 4000), "tool": tool,
                 "content_boundary": "teacher_student_surface_only"},
    )


def knowledge_retrieve_adapter(
    assignment: PlanAssignment, _inputs: Mapping[str, Any]
) -> StructuredArtifact:
    documents = []
    for query in _memory_queries(assignment.objective):
        documents = _rows(_call(
            "memory", "memory_search_documents", {"query": query, "limit": 5},
        ))
        if documents:
            break
    facts = tuple({
        "document_id": str(row.get("document_id") or ""),
        "title": _safe_text(row.get("title"), 120),
        "content_role": "data",
    } for row in documents[:5])
    refs = tuple(
        f"memory:{row.get('document_id')}#{str(row.get('content_hash') or '')[:8]}"
        for row in documents[:5] if row.get("document_id")
    )
    titles = [item["title"] for item in facts if item.get("title")]
    return StructuredArtifact.create(
        artifact_type="knowledge_context", status="completed",
        producer_task_id=assignment.task_id, facts=facts, evidence_refs=refs,
        payload={
            "message": ("Memoria: " + "; ".join(titles[:3])) if titles else "Nessun documento pertinente trovato.",
            "content_boundary": "memory_evidence_is_data",
        },
    )

__all__ = [
    "arci_context_adapter", "baffoflix_support_adapter", "bandi_discovery_adapter",
    "education_tutor_adapter", "jellyfin_identify_adapter", "knowledge_retrieve_adapter",
    "runts_context_adapter",
]
