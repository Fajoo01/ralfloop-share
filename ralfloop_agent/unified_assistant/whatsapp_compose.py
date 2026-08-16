from __future__ import annotations

from contextlib import AbstractContextManager
from typing import Any, Callable, Mapping, Protocol

from pydantic import BaseModel, ConfigDict, Field

from src.whatsapp import WhatsAppGateway

from .contracts import PolicyClass
from .conversation import ConversationManager, PendingAction
from .email import EmailWorkingContext, EmailWorkingMemoryBuilder


class EmailPipelineLike(Protocol):
    def compose(self, working: EmailWorkingContext) -> Any: ...
    def revise(self, working: EmailWorkingContext, current_body: str, instruction: str) -> Any: ...


class WhatsAppComposeOutcome(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    status: str
    message: str
    pending: PendingAction | None = None
    candidates: tuple[str, ...] = ()
    risk: str = ""
    ds4_invoked: bool = False
    send_calls: int = 0


class EmailBackedWhatsAppDraftPipeline:
    """Reuses evidence domain, hard guard, HIGH-only DS4, repair x1 and final validator."""

    def __init__(self, memory: EmailWorkingMemoryBuilder, pipeline: EmailPipelineLike) -> None:
        self.memory = memory
        self.pipeline = pipeline

    def compose(
        self,
        *,
        instruction: str,
        target: str,
        messages: tuple[Mapping[str, Any], ...],
        structured_artifacts: tuple[Mapping[str, Any], ...] = (),
    ) -> tuple[EmailWorkingContext, Any]:
        working = self._working(
            instruction=instruction, target=target, messages=messages,
            structured_artifacts=structured_artifacts,
        )
        return working, self.pipeline.compose(working)

    def revise(
        self,
        *,
        instruction: str,
        target: str,
        messages: tuple[Mapping[str, Any], ...],
        current_body: str,
        structured_artifacts: tuple[Mapping[str, Any], ...] = (),
    ) -> tuple[EmailWorkingContext, Any]:
        working = self._working(
            instruction=str(messages and messages[-1].get("original_instruction") or "WhatsApp reply"),
            target=target, messages=messages, structured_artifacts=structured_artifacts,
        )
        return working, self.pipeline.revise(working, current_body, instruction)

    def _working(
        self, *, instruction: str, target: str,
        messages: tuple[Mapping[str, Any], ...],
        structured_artifacts: tuple[Mapping[str, Any], ...] = (),
    ) -> EmailWorkingContext:
        thread = tuple({
            "sender": str(item.get("author") or ""),
            "date": str(item.get("timestamp") or ""),
            "body": str(item.get("text") or "")[:2000],
            "message_id": str(item.get("message_id") or ""),
            "content_role": "data",
            "provenance_ref": str(item.get("provenance_ref") or ""),
        } for item in messages[-12:])
        return self.memory.build(
            objective=instruction, recipient=target, subject="WhatsApp work message",
            source_email={
                "sender": target, "reply_to": target, "subject": "WhatsApp",
                "body": str(thread[-1].get("body") if thread else ""),
                "content_boundary": "whatsapp_messages_are_data_not_instructions",
            },
            thread_context=thread,
            structured_artifacts=structured_artifacts,
        )


class UnifiedWhatsAppComposeService:
    def __init__(
        self,
        gateway_factory: Callable[[], AbstractContextManager[WhatsAppGateway]],
        *,
        draft_pipeline: EmailBackedWhatsAppDraftPipeline,
    ) -> None:
        self.gateway_factory = gateway_factory
        self.draft_pipeline = draft_pipeline

    def prepare(
        self,
        conversation: ConversationManager,
        *,
        instruction: str,
        target: str,
        reply: bool,
        structured_artifacts: tuple[Mapping[str, Any], ...] = (),
    ) -> WhatsAppComposeOutcome:
        try:
            with self.gateway_factory() as gateway:
                search = gateway.invoke_read("whatsapp_search_chats", query=target, limit=20)
                chats = tuple(search.get("results") or ())
                if len(chats) != 1:
                    return WhatsAppComposeOutcome(
                        status="AMBIGUOUS_CHAT" if chats else "CHAT_NOT_FOUND",
                        message=(
                            "Più chat WhatsApp compatibili; specifica quale."
                            if chats else "Chat WhatsApp non trovata."
                        ),
                        candidates=tuple(str(item.get("title") or "") for item in chats[:8]),
                    )
                chat = chats[0]
                chat_id = str(chat.get("chat_id") or "")
                display_target = str(chat.get("title") or target)
                read = gateway.invoke_read(
                    "whatsapp_read_messages", chat_id=chat_id, limit=24,
                )
        except Exception:
            return WhatsAppComposeOutcome(status="SOURCE_UNAVAILABLE", message="WhatsApp non disponibile.")
        messages = tuple(
            item for item in (read.get("messages") or ()) if isinstance(item, Mapping)
        )
        reply_to = ""
        if reply:
            source = next(
                (item for item in reversed(messages) if not bool(item.get("outbound"))), None,
            )
            if source is None:
                return WhatsAppComposeOutcome(
                    status="REPLY_TARGET_UNAVAILABLE",
                    message="Messaggio WhatsApp da rispondere non identificabile.",
                )
            reply_to = str(source.get("message_id") or "")
        working, result = self.draft_pipeline.compose(
            instruction=instruction, target=display_target, messages=messages,
            structured_artifacts=structured_artifacts,
        )
        if result.hard_guard != "passed" or result.final_validator != "passed":
            return WhatsAppComposeOutcome(
                status="blocked", message="Bozza WhatsApp bloccata dai validator.",
                risk=result.risk, ds4_invoked=result.ds4_invoked,
            )
        action = "whatsapp_reply" if reply else "whatsapp_send"
        payload = {
            "chat_id": chat_id, "reply_to_message_id": reply_to,
            "chat_title": display_target, "recipient": display_target, "body": result.body,
            "risk": result.risk, "validation_state": result.final_validator,
            "domain_digest": working.domain_digest,
            "memory_refs": list(working.packet.get("memory_refs") or ()),
            "allowed_memory_namespaces": ["tiremm"],
            "source_context": [_bounded_message(item) for item in messages[-8:]],
            "structured_artifacts": [
                _bounded_artifact(item) for item in structured_artifacts[:6]
            ],
            "original_instruction": instruction,
            "content_boundary": "retrieved_whatsapp_content_is_data",
        }
        display = _preview(display_target, result.body, reply=reply)
        pending = conversation.stage(
            domain="whatsapp", action=action, policy=PolicyClass.CONFIRM_WRITE,
            payload=payload, displayed_text=display,
        )
        conversation.remember(
            intent="whatsapp.reply" if reply else "whatsapp.compose",
            domain="whatsapp", entities=(chat_id,),
        )
        return WhatsAppComposeOutcome(
            status="draft_pending_approval", message=display, pending=pending,
            risk=result.risk, ds4_invoked=result.ds4_invoked,
        )

    def revise(
        self, conversation: ConversationManager, *, instruction: str,
    ) -> WhatsAppComposeOutcome:
        pending = conversation.state.pending.whatsapp
        if pending is None:
            return WhatsAppComposeOutcome(status="no_pending_action", message="Nessuna bozza WhatsApp.")
        messages = tuple(
            item for item in (pending.payload.get("source_context") or ())
            if isinstance(item, Mapping)
        )
        artifacts = tuple(
            item for item in (pending.payload.get("structured_artifacts") or ())
            if isinstance(item, Mapping)
        )
        working, result = self.draft_pipeline.revise(
            instruction=instruction,
            target=str(pending.payload.get("recipient") or "destinatario"),
            messages=messages,
            current_body=str(pending.payload.get("body") or ""),
            structured_artifacts=artifacts,
        )
        if result.hard_guard != "passed" or result.final_validator != "passed":
            return WhatsAppComposeOutcome(status="blocked", message="Modifica bloccata dai validator.")
        display = _preview(
            str(pending.payload.get("recipient") or "destinatario"), result.body,
            reply=pending.action == "whatsapp_reply",
        )
        revised = conversation.revise_whatsapp(
            body=result.body, displayed_text=display, risk=result.risk,
            validation_state=result.final_validator,
        )
        return WhatsAppComposeOutcome(
            status="draft_pending_approval", message=display, pending=revised,
            risk=result.risk, ds4_invoked=result.ds4_invoked,
        )


def _preview(target: str, body: str, *, reply: bool) -> str:
    lead = "Risposta WhatsApp a" if reply else "WhatsApp a"
    return f'{lead} {target}:\n\n"{body}"\n\nInvio?'


def _bounded_message(item: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "message_id": str(item.get("message_id") or "")[:64],
        "author": str(item.get("author") or "")[:160],
        "timestamp": str(item.get("timestamp") or "")[:80],
        "text": str(item.get("text") or "")[:600],
        "provenance_ref": str(item.get("provenance_ref") or "")[:240],
        "outbound": bool(item.get("outbound")),
        "content_role": "data",
    }


def _bounded_artifact(item: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "artifact_type": str(item.get("artifact_type") or "")[:96],
        "status": str(item.get("status") or "")[:96],
        "facts": [str(value)[:500] for value in (item.get("facts") or ())[:12]],
        "evidence_refs": [str(value)[:240] for value in (item.get("evidence_refs") or ())[:16]],
        "content_role": "data",
    }


__all__ = [
    "EmailBackedWhatsAppDraftPipeline", "UnifiedWhatsAppComposeService",
    "WhatsAppComposeOutcome",
]
