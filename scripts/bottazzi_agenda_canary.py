#!/usr/bin/env python3
from __future__ import annotations

from datetime import datetime
import json
import os
from pathlib import Path
from zoneinfo import ZoneInfo

from ralfloop_agent.unified_assistant.agenda import (
    AgendaPipeline,
    AgendaSource,
    AgendaStore,
    LocalIcsCalendarProvider,
)
from ralfloop_agent.unified_assistant.memory_service import MemoryService
from ralfloop_agent.unified_assistant.task_queue import BotTazziTaskQueue


def main() -> int:
    state_root = Path(os.getenv("BOTTAZZI_AGENDA_CANARY_ROOT", "/home/bandi/.local/share/bottazzi/runtime-production"))
    state_root.mkdir(parents=True, exist_ok=True)
    memory = MemoryService(state_root / "operational-memory.sqlite3")
    store = AgendaStore(state_root / "agenda.sqlite3")
    queue = BotTazziTaskQueue.from_env()
    calendar = LocalIcsCalendarProvider(state_root / "calendar")
    pipeline = AgendaPipeline(store=store, queue=queue, memory=memory, calendar=calendar)
    now = datetime.now(ZoneInfo("Europe/Rome"))
    stamp = now.date().isoformat()

    task_result = pipeline.process(AgendaSource(
        channel="email",
        sender="Bot-tazzi Agenda canary",
        native_id=f"agenda-canary-task-{stamp}",
        timestamp=now,
        original_text="Ricordami di verificare il canary Bot-tazzi Agenda domani",
    ))
    event_result = pipeline.process(AgendaSource(
        channel="whatsapp",
        sender="Bot-tazzi Agenda canary",
        native_id=f"agenda-canary-event-{stamp}",
        timestamp=now,
        original_text="Ci vediamo domani alle 18 per il canary Bot-tazzi Agenda",
    ))
    task = queue.get_task(task_result.outcome_id or "")
    event_path = Path(event_result.calendar_receipt.locator) if event_result.calendar_receipt else Path("/")
    payload = {
        "task": {
            "outcome_id": task_result.outcome_id,
            "duplicate": task_result.duplicate,
            "persisted": task.task_id == task_result.outcome_id,
            "classifier_source": task.classifier_source,
            "human_rank": task.human_rank,
            "deadline_epoch": task.deadline_epoch,
        },
        "appointment": {
            "outcome_id": event_result.outcome_id,
            "duplicate": event_result.duplicate,
            "provider": event_result.calendar_receipt.provider if event_result.calendar_receipt else None,
            "persisted": event_path.is_file(),
            "locator": str(event_path),
            "start_at": event_result.candidate.start_at.isoformat() if event_result.candidate.start_at else None,
        },
        "agenda_db": str(store.path),
        "memory_db": str(memory.path),
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    memory.close()
    if not payload["task"]["persisted"] or not payload["appointment"]["persisted"]:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
