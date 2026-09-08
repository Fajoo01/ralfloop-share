from __future__ import annotations

import re
import os
from pathlib import Path
from typing import Any, Literal, Mapping

from pydantic import BaseModel, ConfigDict, Field

from .operational_runtime import BottazziOperationalRuntime
from .pec_browser_adapter import PecAuthenticatedBrowserAdapter, PecAuthenticatedCdpTransport
from .pec_runts import PecReadProvider, RuntsAuthBoundaryProvider, RuntsReadProvider
from .pec_runts_mcp import capability_descriptors
from .platform import CapabilityPermission, CapabilityRegistry


PEC_RUNTS_REFERENCE = re.compile(
    r"\bpec\b.*\b(?:pratica\s+)?runts\s+(?P<reference>[A-Za-z0-9_.:/-]{3,240})\b",
    re.IGNORECASE,
)

PEC_GENERIC_READ = re.compile(
    r"\b(?:cosa\s+dice|controlla|leggi|guarda|verifica|"
    r"ci\s+sono|cerca|trova)\b.*\bpec\b"
    r"|\bpec\b.*\b(?:appena\s+arrivat[ao]|ultima|ultime|"
    r"pi[uù]\s+recente|nuov[ae]|leggi|controlla|guarda)\b",
    re.IGNORECASE,
)


class PecInboxArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")
    limit: int = Field(default=1, ge=1, le=100)


class PecInboxDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["tool"] = "tool"
    response: None = None
    tool_id: Literal["pec_discover_messages"] = "pec_discover_messages"
    arguments: PecInboxArguments = Field(default_factory=PecInboxArguments)
    mode: Literal["latest", "unread", "search"] = "latest"
    search_term: str | None = Field(default=None, max_length=240)


class PecRuntsDecisionArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")
    runts_reference: str = Field(pattern=r"^[A-Za-z0-9_.:@/-]+$", min_length=1, max_length=240)
    limit: int = Field(default=100, ge=1, le=100)


class PecRuntsToolDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["tool"]
    response: None = None
    tool_id: Literal["pec_find_by_runts_reference"]
    arguments: PecRuntsDecisionArguments


class RuntsResponseArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    practice_id: str = Field(pattern=r"^[A-Za-z0-9_.:@/-]{1,240}$")


class RuntsResponseDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    action: Literal["tool"] = "tool"
    response: None = None
    tool_id: Literal["runts_prepare_practice_response"]
    arguments: RuntsResponseArguments


def decision_for_telegram(
    text: str,
) -> PecInboxDecision | PecRuntsToolDecision | RuntsResponseDecision | None:
    normalized = " ".join(text.split())

    refs = re.findall(
        r"\bpratica\s+RUNTS\s+([A-Za-z0-9_.:/-]+)",
        text,
        re.I,
    )
    if (
        refs
        and re.search(r"\bprepara\b", text, re.I)
        and re.search(r"\brisposta\b", text, re.I)
    ):
        if len(set(refs)) != 1:
            raise ValueError("ambiguous_practice_reference")
        candidates = CapabilityRegistry(capability_descriptors()).retrieve(
            text,
            domains=("pec_runts",),
            allowed_permissions=(CapabilityPermission.PROPOSE,),
            limit=3,
        )
        compatible = [
            c
            for c in candidates
            if c.input_schema.get("required") == ["practice_id"]
        ]
        if len(compatible) != 1:
            raise ValueError("runts_prepare_capability_unresolved")
        return RuntsResponseDecision(
            tool_id=compatible[0].capability_id,
            arguments=RuntsResponseArguments(practice_id=refs[0]),
        )

    match = PEC_RUNTS_REFERENCE.search(normalized)
    if match:
        candidates = CapabilityRegistry(capability_descriptors()).retrieve(
            text,
            domains=("pec_runts",),
            allowed_permissions=(CapabilityPermission.READ,),
            limit=3,
        )
        compatible = [
            row
            for row in candidates
            if "runts_reference"
            in row.input_schema.get("required", ())
        ]
        if len(compatible) != 1:
            raise ValueError("pec_runts_capability_unresolved")
        return PecRuntsToolDecision(
            action="tool",
            tool_id=compatible[0].capability_id,
            arguments=PecRuntsDecisionArguments(
                runts_reference=match.group("reference")
            ),
        )

    if PEC_GENERIC_READ.search(normalized):
        if re.search(
            r"\b(?:nuov[ae]|non\s+lett[ae])\s+pec\b"
            r"|\bpec\b.*\b(?:nuov[ae]|non\s+lett[ae])\b",
            normalized,
            re.I,
        ):
            return PecInboxDecision(
                arguments=PecInboxArguments(limit=100),
                mode="unread",
            )

        search_match = re.search(
            r"\bpec\s+(?:di|da)\s+(.+?)(?:\?|!|$)",
            normalized,
            re.I,
        )
        if search_match:
            search_term = search_match.group(1).strip(" .?!")
            if search_term:
                return PecInboxDecision(
                    arguments=PecInboxArguments(limit=100),
                    mode="search",
                    search_term=search_term,
                )

        return PecInboxDecision(
            arguments=PecInboxArguments(limit=1),
            mode="latest",
        )

    return None


def _format_pec_message(message: Mapping[str, Any]) -> str:
    sender = str(message.get("sender") or "non disponibile")
    subject = str(message.get("subject") or "non disponibile")
    received_at = str(message.get("received_at") or "non disponibile")

    body = " ".join(str(message.get("body") or "").split())
    if len(body) > 1800:
        body = body[:1800].rstrip() + "…"
    if not body:
        body = "corpo non disponibile"

    attachments = list(message.get("attachments") or ())
    names = [
        str(item.get("filename") or item.get("attachment_id") or "allegato")
        for item in attachments
        if isinstance(item, Mapping)
    ]

    deadline_sentences = []
    original_body = str(message.get("body") or "")
    for sentence in re.split(r"(?<=[.!?])\s+|\n+", original_body):
        clean = " ".join(sentence.split())
        folded = clean.casefold()
        if clean and any(
            token in folded
            for token in (
                "scaden",
                "entro ",
                "termine",
                "quanto prima",
                "giorni dalla",
                "giorni dal",
            )
        ):
            deadline_sentences.append(clean)
        if len(deadline_sentences) >= 3:
            break

    deadlines = (
        " ".join(deadline_sentences)
        if deadline_sentences
        else "Nessuna scadenza esplicita individuata."
    )

    return (
        f"Mittente: {sender}\n"
        f"Oggetto: {subject}\n"
        f"Data/ora: {received_at}\n"
        f"Cosa comunica: {body}\n"
        f"Scadenze/azioni: {deadlines}\n"
        f"Allegati: {', '.join(names) if names else 'nessuno'}"
    )


def execute_telegram_read(
    text: str,
    *,
    memory_path: str | Path,
    pec_provider: PecReadProvider | None = None,
    runts_provider: RuntsReadProvider | None = None,
    runts_response_preparer=None,
) -> dict[str, Any]:
    decision = decision_for_telegram(text)
    if decision is None:
        raise ValueError("pec_runts_telegram_not_applicable")
    if isinstance(decision,RuntsResponseDecision):
        return execute_telegram_prepare(text,memory_path=memory_path,pec_provider=pec_provider,
            runts_provider=runts_provider,response_preparer=runts_response_preparer)
    transport = None
    managed_pec_provider = None
    if pec_provider is None:
        from .pec_provider_factory import (
            build_default_pec_provider,
            imap_environment_present,
        )
        if (
            imap_environment_present()
            or os.getenv("BOTTAZZI_PEC_READ_PROVIDER", "").strip()
        ):
            managed_pec_provider = build_default_pec_provider()
            pec_provider = managed_pec_provider
        else:
            # Preserve the existing browser path exactly when
            # IMAP has not yet been configured.
            transport = PecAuthenticatedCdpTransport()
            pec_provider = PecAuthenticatedBrowserAdapter(transport)
    try:
        with BottazziOperationalRuntime(
            memory_path, pec_provider=pec_provider,
            runts_provider=runts_provider or RuntsAuthBoundaryProvider(),
        ) as runtime:
            query = (
                "pec posta certificata nuovi messaggi ricevuti"
                if isinstance(decision, PecInboxDecision)
                else text
            )
            result = runtime.invoke_pec_runts(
                query,
                decision.arguments.model_dump(),
            )
        payload = dict(result.get("structuredContent") or {})
        messages = list(payload.get("messages") or ())

        if result.get("isError"):
            answer = (
                "Ricerca PEC non completata: "
                f"{payload.get('status', 'SOURCE_UNAVAILABLE')}. "
                "Nessuna operazione di scrittura eseguita."
            )
        elif isinstance(decision, PecInboxDecision):
            if decision.mode == "latest":
                if not messages:
                    answer = "Non risultano PEC nella casella."
                else:
                    answer = (
                        "Ultima PEC ricevuta:\n"
                        + _format_pec_message(messages[0])
                    )
                    messages = messages[:1]

            elif decision.mode == "unread":
                unread = [
                    row
                    for row in messages
                    if row.get("unread") is True
                ]
                if not unread:
                    answer = "Non risultano nuove PEC non lette."
                    messages = []
                else:
                    answer = (
                        f"Risultano {len(unread)} PEC non lette. "
                        "La più recente è:\n"
                        + _format_pec_message(unread[0])
                    )
                    messages = unread

            else:
                term = (decision.search_term or "").casefold()
                matches = [
                    row
                    for row in messages
                    if term
                    and term
                    in (
                        str(row.get("sender") or "")
                        + "\n"
                        + str(row.get("subject") or "")
                        + "\n"
                        + str(row.get("body") or "")
                    ).casefold()
                ]

                if not matches:
                    answer = (
                        "Non ho trovato una PEC corrispondente a "
                        f"{decision.search_term!r}."
                    )
                    messages = []
                else:
                    answer = (
                        f"Trovate {len(matches)} PEC corrispondenti. "
                        "La più recente è:\n"
                        + _format_pec_message(matches[0])
                    )
                    messages = matches

        elif messages:
            answer = (
                f"Trovate {len(messages)} comunicazioni PEC con "
                f"riferimento RUNTS esatto "
                f"{decision.arguments.runts_reference}."
            )
        else:
            answer = (
                "Nessuna comunicazione PEC con riferimento RUNTS esatto "
                f"{decision.arguments.runts_reference}."
            )
        return {
            "ok": not bool(result.get("isError")),
            "interaction_mode": "unified_assistant",
            "capability": decision.tool_id,
            "tools_executed": True,
            "response": answer,
            "final_answer": answer,
            "approval_required": False,
            "metadata": {
                "tool_decision": "tool", "tool_decision_valid": True,
                "selected_capability": result.get("selectedCapability"),
                "mcp_invoked": True, "read_result": payload,
                "writes": payload.get("writes", 0),
            },
            "artifacts": [{"artifact_type": "pec_runts_read", "status": "completed" if not result.get("isError") else payload.get("status"), "count": len(messages), "writes": 0}],
            "audit_summary": [f"pec_runts::{decision.tool_id}::read_only"],
        }
    finally:
        if transport is not None:
            transport.close()
        if managed_pec_provider is not None:
            close = getattr(managed_pec_provider, "close", None)
            if close is not None:
                close()


def is_pec_runts_telegram(text: str) -> bool:
    return decision_for_telegram(text) is not None


def execute_telegram_prepare(text, *, memory_path, pec_provider=None, runts_provider=None, response_preparer=None):
    decision=decision_for_telegram(text)
    if not isinstance(decision,RuntsResponseDecision):raise ValueError("runts_prepare_not_applicable")
    transports=[]
    managed_pec_providers=[]
    if response_preparer is None and os.getenv("BOTTAZZI_RUNTS_PREPARE_BINDING"):
        from .runts_response_prepare import RuntsPrepareBinding, RuntsSuiteResponsePreparer
        response_preparer=RuntsSuiteResponsePreparer(RuntsPrepareBinding.model_validate_json(Path(os.environ["BOTTAZZI_RUNTS_PREPARE_BINDING"]).read_text()))
    if runts_provider is None and response_preparer is not None:
        # Existing authenticated READ adapter; never a generic browser executor.
        # Its separate release must be promoted along with a production binding.
        from .runts_browser_adapter import RuntsAuthenticatedBrowserAdapter, RuntsAuthenticatedCdpTransport
        runts_provider=RuntsAuthenticatedBrowserAdapter(RuntsAuthenticatedCdpTransport())
    if pec_provider is None:
        from .pec_provider_factory import (
            build_default_pec_provider,
            imap_environment_present,
        )
        if (
            imap_environment_present()
            or os.getenv("BOTTAZZI_PEC_READ_PROVIDER", "").strip()
        ):
            pec_provider=build_default_pec_provider()
            managed_pec_providers.append(pec_provider)
        else:
            transport=PecAuthenticatedCdpTransport();transports.append(transport)
            pec_provider=PecAuthenticatedBrowserAdapter(transport)
    try:
        with BottazziOperationalRuntime(memory_path,pec_provider=pec_provider,runts_provider=runts_provider or RuntsAuthBoundaryProvider(),runts_response_preparer=response_preparer) as runtime:
            pec=runtime.invoke_pec_runts("cerca PEC riferimento pratica RUNTS",{"runts_reference":decision.arguments.practice_id,"limit":100})
            # A PEC notification is optional; RUNTS remains authoritative.
            result=runtime.invoke_pec_runts(text,decision.arguments.model_dump())
            proposal=(result.get("structuredContent") or {}).get("proposal")
            ok=bool(proposal) and not result.get("isError")
            practice_status = (
                runts_provider.get_practice(
                    decision.arguments.practice_id
                ).status_raw
                if ok else ""
            )
            answer=(proposal["proposed_reply"]+"\nStato: "+proposal["status"]+". Nessun invio eseguito." if ok else "Preparazione non completata: "+result.get("structuredContent",{}).get("status","SOURCE_UNAVAILABLE"))
            if ok:
                answer+="\nPDF: "+proposal["review_context"]["pdf_path"]+"\nSHA256: "+proposal["sha256"]
                if pec.get("isError"):
                    answer+="\nPEC non disponibile in questa verifica; utilizzata la comunicazione RUNTS autoritativa."
            return {"ok":ok,"response":answer,"final_answer":answer,"interaction_mode":"unified_assistant",
                # Administrative review is not an executable pending confirmation.
                # Legacy Meowgram hides the full answer when approval_required is true.
                "capability":decision.tool_id,"tools_executed":True,"approval_required":False,"human_review_required":ok,
                "metadata":{"tool_decision_valid":True,"selected_capability":result.get("selectedCapability"),
                    "mcp_invoked":True,"writes":0,"proposal":proposal,"pec_status":pec.get("structuredContent",{}).get("status","READ"),
                    "practice_status":practice_status,
                    "mcp_calls":[pec.get("selectedCapability"),result.get("selectedCapability")],"deployment_state":"TESTED_WORKING_TREE"},
                "artifacts":list(proposal["attachments"]) if ok else [],"audit_summary":["RUNTS_PREPARE_STOP_BEFORE_WRITE"]}
    finally:
        for transport in transports:transport.close()
        for provider in managed_pec_providers:
            close=getattr(provider,"close",None)
            if close is not None:close()


__all__ = [
    "PecInboxDecision",
    "PecRuntsToolDecision",
    "decision_for_telegram",
    "execute_telegram_read",
    "is_pec_runts_telegram",
]
