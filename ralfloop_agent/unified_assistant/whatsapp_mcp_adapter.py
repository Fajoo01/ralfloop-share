from __future__ import annotations

from contextlib import AbstractContextManager
import re
from typing import Callable, Mapping

from src.whatsapp import WhatsAppGateway

from .whatsapp_web import WhatsAppEvidence, WhatsAppReadResult


class WhatsAppMCPReadOnly:
    def __init__(
        self, gateway_factory: Callable[[], AbstractContextManager[WhatsAppGateway]],
    ) -> None:
        self.gateway_factory = gateway_factory

    def read(
        self, request: str, *, allowed_namespaces: tuple[str, ...],
    ) -> WhatsAppReadResult:
        if "tiremm" not in allowed_namespaces:
            return _result("POLICY_DENIED", "Namespace WhatsApp non autorizzato.")
        query = " ".join(request.split())[:4000]
        try:
            with self.gateway_factory() as gateway:
                chat_hint = _chat_hint(request)
                chat_id = ""
                if chat_hint:
                    found = tuple(gateway.invoke_read(
                        "whatsapp_search_chats", query=chat_hint, limit=20,
                    ).get("results") or ())
                    if len(found) > 1:
                        return _result("AMBIGUOUS_CHAT", "Più chat compatibili; specifica quale.")
                    if not found:
                        return _result("CHAT_NOT_FOUND", "Chat WhatsApp non trovata.")
                    chat_id = str(found[0].get("chat_id") or "")
                    query = " ".join(request.replace(chat_hint, "", 1).split())[:4000]
                args = {"query": query, "limit": 100}
                if chat_id:
                    args["chat_id"] = chat_id
                raw = gateway.invoke_read("whatsapp_search_messages", **args)
        except Exception:
            return _result("SOURCE_UNAVAILABLE", "WhatsApp MCP non disponibile.")
        evidence: list[WhatsAppEvidence] = []
        for item in raw.get("evidence") or ():
            if not isinstance(item, Mapping):
                continue
            try:
                evidence.append(WhatsAppEvidence.model_validate(item))
            except ValueError:
                continue
        status = str(raw.get("status") or (
            "FOUND" if evidence else "SEARCH_INCOMPLETE"
        ))
        if status not in {
            "FOUND", "NOT_FOUND_IN_SEARCHED_SCOPE", "SEARCH_INCOMPLETE",
            "AUTH_REQUIRED", "SOURCE_UNAVAILABLE",
        }:
            status = "SEARCH_INCOMPLETE"
        complete = bool(raw.get("search_complete"))
        response = (
            f"Trovati {len(evidence)} messaggi WhatsApp nello scope esplorato."
            if evidence else
            ("Nessun messaggio nello scope completamente esplorato." if complete else
             "Ricerca WhatsApp incompleta; nessuna conclusione assoluta.")
        )
        return WhatsAppReadResult(
            status=status, session_authenticated=status != "AUTH_REQUIRED",
            chat_ref=str(raw.get("chat_id") or "") or None,
            visible_items=tuple(item.text for item in evidence if item.text),
            evidence=tuple(evidence),
            provenance=tuple(item.provenance_ref for item in evidence),
            read_operations=("whatsapp_search_messages",),
            search_complete=complete,
            messages_seen=int(raw.get("messages_seen") or len(evidence)),
            scroll_iterations=int(raw.get("scroll_iterations") or 0),
            history_exhausted=bool(raw.get("history_exhausted")),
            cap_reached=bool(raw.get("cap_reached")),
            response=response,
        )


def _chat_hint(request: str) -> str:
    match = re.search(
        r"\b(?:chat|conversazione)\s+con\s+([\wÀ-ÿ'. -]{2,80}?)(?=[,:.!?]|\s+(?:su|che)\b|$)",
        request, re.I,
    )
    return " ".join(match.group(1).split()) if match else ""


def _result(status: str, response: str) -> WhatsAppReadResult:
    return WhatsAppReadResult(
        status=status, session_authenticated=status != "AUTH_REQUIRED", response=response,
    )


__all__ = ["WhatsAppMCPReadOnly"]
