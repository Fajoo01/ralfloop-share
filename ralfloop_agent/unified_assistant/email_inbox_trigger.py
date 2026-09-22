from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parseaddr, parsedate_to_datetime
from pathlib import Path
from typing import Any, Callable, Mapping

from ralfloop_agent.cli.session_store import SessionStore, SessionStoreError

from .conversation import SessionConversationAdapter
from .event_router import EventOrigin, EventRouter, RoutedEvent, event_id
from .memory_service import MemoryEntity, MemoryService
from .platform import SourceRef


@dataclass(frozen=True)
class GmailTriggerResult:
    bootstrapped: bool
    discovered: int
    queued: int
    skipped: int
    drafted: int
    busy: bool


class GmailInboxTrigger:
    """Poll Gmail read-only and stage at most one approval-bound reply draft at a time."""

    def __init__(
        self,
        *,
        gateway_factory: Callable[[], Any],
        memory: MemoryService,
        router: EventRouter,
        response_runner: Callable[[str, Mapping[str, Any]], Mapping[str, Any]],
        account: str,
        telegram_user_id: int,
        telegram_chat_id: int,
        session_root: str | Path,
        state_path: str | Path,
        lookback: str = "2d",
        max_results: int = 50,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        if telegram_user_id <= 0 or telegram_chat_id <= 0:
            raise ValueError("gmail_trigger_telegram_identity_required")
        self.gateway_factory = gateway_factory
        self.memory = memory
        self.router = router
        self.response_runner = response_runner
        self.account = account.casefold().strip()
        self.telegram_user_id = telegram_user_id
        self.telegram_chat_id = telegram_chat_id
        self.session_root = Path(session_root)
        self.state_path = Path(state_path)
        self.lookback = lookback
        self.max_results = max(1, min(int(max_results), 50))
        self.now = now or (lambda: datetime.now(UTC))

    def poll(self) -> GmailTriggerResult:
        rows = self._search()
        ids = [str(row.get("messageId") or "") for row in rows]
        ids = [item for item in ids if item]
        state = self._load_state()
        if state is None:
            self._save_state({
                "schema_version": "gmail_inbox_trigger_v1",
                "activated_at": self.now().isoformat(),
                "known_message_ids": ids,
            })
            return GmailTriggerResult(True, 0, 0, 0, 0, self._email_pending())

        known = set(map(str, state.get("known_message_ids") or ()))
        activated_at = _parse_datetime(str(state.get("activated_at") or "")) or self.now()
        discovered = queued = skipped = 0
        for message_id in ids:
            if message_id in known:
                continue
            discovered += 1
            known.add(message_id)
            detail = self._read(message_id)
            message = detail.get("message") if isinstance(detail, Mapping) else None
            if not isinstance(message, Mapping):
                skipped += 1
                continue
            received = _parse_datetime(str(message.get("date") or ""))
            if received is not None and received < activated_at:
                skipped += 1
                continue
            candidate = self._candidate(message)
            if candidate is None:
                skipped += 1
                continue
            if self.memory.put_entity(candidate):
                queued += 1
            self.router.route(self._event(message))

        self._save_state({
            "schema_version": "gmail_inbox_trigger_v1",
            "activated_at": activated_at.isoformat(),
            "known_message_ids": sorted(known)[-5000:],
        })
        busy = self._email_pending()
        drafted = 0 if busy else self._drain_one()
        return GmailTriggerResult(False, discovered, queued, skipped, drafted, busy)

    def _search(self) -> list[Mapping[str, Any]]:
        with self.gateway_factory() as gateway:
            result = gateway.invoke(
                "search", query=f"in:inbox newer_than:{self.lookback}",
                maxResults=self.max_results,
            )
        rows = result.get("messages") if isinstance(result, Mapping) else None
        return [row for row in rows if isinstance(row, Mapping)] if isinstance(rows, list) else []

    def _read(self, message_id: str) -> Mapping[str, Any]:
        with self.gateway_factory() as gateway:
            return gateway.invoke("read", messageId=message_id)

    def _candidate(self, message: Mapping[str, Any]) -> MemoryEntity | None:
        message_id = str(message.get("messageId") or "")
        display, address = parseaddr(str(message.get("from") or ""))
        address = address.casefold()
        labels = {item.strip().upper() for item in str(message.get("labels") or "").split(",") if item.strip()}
        if not message_id or not address or address == self.account:
            return None
        if any(token in address for token in ("no-reply", "noreply", "do-not-reply", "donotreply")):
            return None
        if labels & {"SPAM", "TRASH", "CATEGORY_PROMOTIONS", "CATEGORY_SOCIAL"}:
            return None
        observed = self.now()
        received = _parse_datetime(str(message.get("date") or "")) or observed
        source = SourceRef(
            system="gmail", native_id=message_id,
            locator=f"gmail:message:{message_id}", observed_at=observed.isoformat(),
        )
        return MemoryEntity.build(
            entity_id=f"email-reply-{message_id}",
            domain="email_intake", entity_type="EMAIL_REPLY_CANDIDATE", status="QUEUED",
            updated_at=received,
            data={
                "message_id": message_id,
                "sender_name": display,
                "sender_address": address,
                "subject": str(message.get("subject") or ""),
                "received_at": received.isoformat(),
            },
            provenance=(source,),
        )

    def _event(self, message: Mapping[str, Any]) -> RoutedEvent:
        message_id = str(message.get("messageId") or "")
        observed = self.now()
        received = _parse_datetime(str(message.get("date") or "")) or observed
        source = SourceRef(
            system="gmail", native_id=message_id,
            locator=f"gmail:message:{message_id}", observed_at=observed.isoformat(),
        )
        payload = {
            "subject": str(message.get("subject") or "")[:500],
            "sender": str(message.get("from") or "")[:500],
        }
        return RoutedEvent(
            event_id=event_id("gmail", message_id, "EMAIL_RECEIVED", payload),
            event_type="EMAIL_RECEIVED", origin=EventOrigin.POLLER,
            source="gmail", source_id=message_id,
            occurred_at=received, observed_at=observed,
            entity_refs=(f"email-reply-{message_id}",),
            payload=payload, provenance=(source,),
        )

    def _email_pending(self) -> bool:
        session_id = f"telegram-{self.telegram_chat_id}-{self.telegram_user_id}"
        try:
            conversation = SessionConversationAdapter(SessionStore(self.session_root)).load(session_id)
        except (OSError, SessionStoreError, ValueError):
            return False
        pending = conversation.state.pending.email
        return pending is not None and pending.expires_at > int(self.now().timestamp())

    def _drain_one(self) -> int:
        queued = list(self.memory.list_entities(domain="email_intake", status="QUEUED", limit=100))
        if not queued:
            return 0
        queued.sort(key=lambda item: (item.updated_at, item.entity_id))
        candidate = queued[0]
        data = dict(candidate.data)
        address = str(data.get("sender_address") or "")
        message_id = str(data.get("message_id") or "")
        if not address or not message_id:
            self._update_candidate(candidate, "FAILED", {"reason": "candidate_identity_missing"})
            return 0
        instruction = (
            "Rispondi alla mail. "
            f"Destinatario verificato: {address}. "
            f"Messaggio Gmail sorgente: {message_id}. "
            "Prepara una risposta pertinente usando solo il contenuto della mail e i fatti verificati di Tiremm Innanz; non inviare senza approvazione."
        )
        context = {
            "source": "telegram_natural",
            "telegram_user_id": self.telegram_user_id,
            "telegram_chat_id": self.telegram_chat_id,
            "telegram_message_id": 0,
            "telegram_chat_type": "private",
        }
        result = dict(self.response_runner(instruction, context))
        metadata = result.get("metadata") if isinstance(result.get("metadata"), Mapping) else {}
        status = str(metadata.get("status") or result.get("stop_reason") or "")
        if status == "draft_pending_approval":
            self._update_candidate(candidate, "PENDING_APPROVAL", {
                "pending_id": metadata.get("pending_id"),
                "approval_request_id": metadata.get("approval_request_id"),
            })
            return 1
        self._update_candidate(candidate, "FAILED", {"reason": status or "draft_not_created"})
        return 0

    def _update_candidate(self, candidate: MemoryEntity, status: str, extra: Mapping[str, Any]) -> None:
        data = {**candidate.data, **dict(extra)}
        updated = MemoryEntity.build(
            entity_id=candidate.entity_id, domain=candidate.domain,
            entity_type=candidate.entity_type, status=status,
            updated_at=max(self.now(), candidate.updated_at), data=data, provenance=candidate.provenance,
        )
        self.memory.put_entity(updated)

    def _load_state(self) -> dict[str, Any] | None:
        try:
            raw = json.loads(self.state_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError("gmail_trigger_state_invalid") from exc
        return raw if isinstance(raw, dict) else None

    def _save_state(self, state: Mapping[str, Any]) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.state_path.with_suffix(self.state_path.suffix + ".tmp")
        temporary.write_text(json.dumps(dict(state), ensure_ascii=False, sort_keys=True), encoding="utf-8")
        temporary.replace(self.state_path)


def _parse_datetime(value: str) -> datetime | None:
    if not value.strip():
        return None
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


__all__ = ["GmailInboxTrigger", "GmailTriggerResult"]
