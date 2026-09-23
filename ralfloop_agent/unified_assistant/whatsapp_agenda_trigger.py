from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import json
from pathlib import Path
from typing import Any, Callable, Mapping

from .agenda_ingress import AgendaIntake


@dataclass(frozen=True)
class WhatsAppAgendaTriggerResult:
    bootstrapped: bool
    chats_seen: int
    messages_seen: int
    ingested: int
    skipped: int


class WhatsAppAgendaTrigger:
    """Incremental read-only WhatsApp poller feeding only unseen messages to Agenda."""

    SELF_AUTHORS = frozenset({"tu", "you", "me", "io", "fabio"})

    def __init__(
        self,
        *,
        gateway_factory: Callable[[], Any],
        agenda_intake: AgendaIntake,
        state_path: str | Path,
        chat_limit: int = 100,
        message_limit: int = 100,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.gateway_factory = gateway_factory
        self.agenda_intake = agenda_intake
        self.state_path = Path(state_path)
        self.chat_limit = max(1, min(int(chat_limit), 500))
        self.message_limit = max(1, min(int(message_limit), 500))
        self.now = now or (lambda: datetime.now(UTC))

    def poll(self) -> WhatsAppAgendaTriggerResult:
        snapshots = self._snapshot()
        all_ids = [str(message.get("message_id") or "") for _, message in snapshots]
        all_ids = [item for item in all_ids if item]
        state = self._load_state()
        if state is None:
            self._save_state({
                "schema_version": "whatsapp_agenda_trigger_v1",
                "activated_at": self.now().isoformat(),
                "known_message_ids": sorted(set(all_ids))[-20000:],
            })
            return WhatsAppAgendaTriggerResult(
                bootstrapped=True,
                chats_seen=len({chat_id for chat_id, _ in snapshots}),
                messages_seen=len(snapshots),
                ingested=0,
                skipped=0,
            )

        known = set(map(str, state.get("known_message_ids") or ()))
        ingested = skipped = 0
        for _chat_id, message in snapshots:
            message_id = str(message.get("message_id") or "")
            if not message_id or message_id in known:
                continue
            known.add(message_id)
            author = str(message.get("author") or "").strip()
            text = str(message.get("text") or "").strip()
            kind = str(message.get("kind") or "text")
            if not text or kind not in {"text", "audio"} or author.casefold() in self.SELF_AUTHORS:
                skipped += 1
                continue
            result = self.agenda_intake.ingest_whatsapp(message, fallback_timestamp=self.now())
            if result is None:
                skipped += 1
            else:
                ingested += 1

        self._save_state({
            "schema_version": "whatsapp_agenda_trigger_v1",
            "activated_at": str(state.get("activated_at") or self.now().isoformat()),
            "known_message_ids": sorted(known)[-20000:],
        })
        return WhatsAppAgendaTriggerResult(
            bootstrapped=False,
            chats_seen=len({chat_id for chat_id, _ in snapshots}),
            messages_seen=len(snapshots),
            ingested=ingested,
            skipped=skipped,
        )

    def _snapshot(self) -> list[tuple[str, Mapping[str, Any]]]:
        rows: list[tuple[str, Mapping[str, Any]]] = []
        with self.gateway_factory() as gateway:
            chats = gateway.invoke_read("whatsapp_list_chats", limit=self.chat_limit)
            for chat in chats.get("results") or ():
                if not isinstance(chat, Mapping):
                    continue
                chat_id = str(chat.get("chat_id") or "")
                if not chat_id:
                    continue
                read = gateway.invoke_read("whatsapp_read_messages", chat_id=chat_id, limit=self.message_limit)
                messages = read.get("messages")
                if not isinstance(messages, list):
                    evidence = read.get("evidence")
                    messages = evidence if isinstance(evidence, list) else []
                for message in messages:
                    if isinstance(message, Mapping):
                        rows.append((chat_id, message))
        return rows

    def _load_state(self) -> dict[str, Any] | None:
        try:
            payload = json.loads(self.state_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError("whatsapp_agenda_trigger_state_invalid") from exc
        return payload if isinstance(payload, dict) else None

    def _save_state(self, state: Mapping[str, Any]) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.state_path.with_suffix(self.state_path.suffix + ".tmp")
        temporary.write_text(json.dumps(dict(state), ensure_ascii=False, sort_keys=True), encoding="utf-8")
        temporary.replace(self.state_path)


__all__ = ["WhatsAppAgendaTrigger", "WhatsAppAgendaTriggerResult"]
