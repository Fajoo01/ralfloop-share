from __future__ import annotations

from datetime import UTC, datetime
from email.utils import parseaddr, parsedate_to_datetime
from typing import Any, Mapping

from .agenda import (
    AgendaPipeline,
    AgendaResult,
    AgendaSource,
    AgendaStore,
    CalendarProvider,
    calendar_provider_from_env,
)
from .memory_service import MemoryService
from .task_queue import BotTazziTaskQueue


def _timestamp(value: Any, fallback: datetime | None = None) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value or "").strip()
        parsed = None
        if text:
            try:
                parsed = parsedate_to_datetime(text)
            except (TypeError, ValueError):
                try:
                    parsed = datetime.fromisoformat(text)
                except ValueError:
                    parsed = None
        if parsed is None:
            parsed = fallback or datetime.now(UTC)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


class AgendaIntake:
    """Single normalization edge for all communication channels."""

    def __init__(self, pipeline: AgendaPipeline) -> None:
        self.pipeline = pipeline

    def _process_source(self, source: AgendaSource) -> AgendaResult | None:
        results = self.pipeline.process_all(source)
        return results[0] if results else None

    def ingest_email(self, message: Mapping[str, Any], *, channel: str = "email") -> AgendaResult | None:
        native_id = str(message.get("messageId") or message.get("message_id") or message.get("id") or "").strip()
        if not native_id:
            return None
        raw_sender = str(message.get("from") or message.get("sender") or "unknown").strip() or "unknown"
        display, address = parseaddr(raw_sender)
        sender = display or address or raw_sender
        subject = str(message.get("subject") or "").strip()
        body = str(message.get("body") or message.get("text") or "").strip()
        text = "\n".join(part for part in (subject, body) if part).strip()
        if not text:
            return None
        return self._process_source(AgendaSource(
            channel=channel,
            sender=sender,
            native_id=native_id,
            timestamp=_timestamp(message.get("date") or message.get("received_at") or message.get("timestamp")),
            original_text=text,
        ))

    def ingest_pec(self, message: Any) -> AgendaResult | None:
        if hasattr(message, "native_id"):
            native_id = str(message.native_id)
            sender = str(message.sender)
            subject = str(message.subject or "")
            body = str(message.body or "")
            received = message.received_at
        elif isinstance(message, Mapping):
            native_id = str(message.get("native_id") or message.get("message_id") or message.get("id") or "")
            sender = str(message.get("sender") or message.get("from") or "unknown")
            subject = str(message.get("subject") or "")
            body = str(message.get("body") or message.get("text") or "")
            received = message.get("received_at") or message.get("timestamp")
        else:
            return None
        text = "\n".join(part.strip() for part in (subject, body) if part and part.strip()).strip()
        if not native_id or not text:
            return None
        return self._process_source(AgendaSource(
            channel="pec", sender=sender or "unknown", native_id=native_id,
            timestamp=_timestamp(received), original_text=text,
        ))

    def ingest_whatsapp(self, evidence: Any, *, fallback_timestamp: datetime | None = None) -> AgendaResult | None:
        if hasattr(evidence, "message_id"):
            native_id = str(evidence.message_id)
            sender = str(evidence.author or "unknown")
            text = str(evidence.text or "").strip()
            timestamp = evidence.timestamp
        elif isinstance(evidence, Mapping):
            native_id = str(evidence.get("message_id") or evidence.get("id") or "")
            sender = str(evidence.get("author") or evidence.get("sender") or "unknown")
            text = str(evidence.get("text") or evidence.get("body") or "").strip()
            timestamp = evidence.get("timestamp")
        else:
            return None
        if not native_id or not text:
            return None
        return self._process_source(AgendaSource(
            channel="whatsapp", sender=sender or "unknown", native_id=native_id,
            timestamp=_timestamp(timestamp, fallback_timestamp), original_text=text,
        ))

    def ingest_call_transcript(self, *, call_id: str, contact: str, timestamp: datetime, transcript: str) -> AgendaResult | None:
        text = " ".join(transcript.split())
        if not call_id.strip() or not text:
            return None
        return self._process_source(AgendaSource(
            channel="phone_call", sender=contact.strip() or "unknown",
            native_id=call_id.strip(), timestamp=_timestamp(timestamp), original_text=text,
        ))


def build_default_agenda_intake(
    memory: MemoryService,
    *,
    queue: BotTazziTaskQueue | None = None,
    calendar: CalendarProvider | None = None,
) -> AgendaIntake:
    from .agenda_motor import agenda_ambiguity_resolver_from_env

    return AgendaIntake(AgendaPipeline(
        store=AgendaStore.from_env(),
        queue=queue or BotTazziTaskQueue.from_env(),
        memory=memory,
        calendar=calendar or calendar_provider_from_env(),
        ambiguity_resolver=agenda_ambiguity_resolver_from_env(),
    ))


__all__ = ["AgendaIntake", "build_default_agenda_intake"]
