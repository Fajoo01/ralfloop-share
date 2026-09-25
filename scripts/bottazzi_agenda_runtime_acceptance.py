#!/usr/bin/env python3
from __future__ import annotations

from datetime import datetime
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from zoneinfo import ZoneInfo

from ralfloop_agent.unified_assistant.agenda import AgendaPipeline, AgendaSource, AgendaStore, calendar_provider_from_env
from ralfloop_agent.unified_assistant.memory_service import MemoryService
from ralfloop_agent.unified_assistant.task_queue import BotTazziTaskQueue


def main() -> int:
    now = datetime.now(ZoneInfo("Europe/Rome"))
    token = now.strftime("%Y%m%dT%H%M%S")
    calendar_dir = Path(os.environ["BOTTAZZI_AGENDA_CALENDAR_DIR"])
    before = {p.name for p in calendar_dir.glob("*.ics")}
    with TemporaryDirectory(prefix="bottazzi-agenda-accept-") as raw:
        root = Path(raw)
        memory = MemoryService(root / "memory.sqlite3")
        store = AgendaStore(root / "agenda.sqlite3")
        queue = BotTazziTaskQueue.from_env()
        pipeline = AgendaPipeline(store=store, queue=queue, memory=memory, calendar=calendar_provider_from_env())

        task = pipeline.process(AgendaSource(channel="email", sender="acceptance", native_id=f"task-{token}", timestamp=now, original_text="Ricordami di controllare l'agenda domani"))
        first = pipeline.process(AgendaSource(channel="email", sender="acceptance", native_id=f"event-mail-{token}", timestamp=now, original_text="Ci vediamo domani alle 19 per verifica agenda runtime"))
        second = pipeline.process(AgendaSource(channel="whatsapp", sender="acceptance", native_id=f"event-wa-{token}", timestamp=now, original_text="Ci vediamo domani alle 19 per verifica agenda runtime"))
        uncertain = pipeline.process(AgendaSource(channel="email", sender="acceptance", native_id=f"amb-{token}", timestamp=now, original_text="Forse ci vediamo domani alle 20 per verifica agenda runtime"))

        task_row = queue.get_task(task.outcome_id or "")
        sources = store.sources(first.dedup_key)
        after = {p.name for p in calendar_dir.glob("*.ics")}
        created = sorted(after - before)
        payload = {
            "task_persisted": task_row.task_id == task.outcome_id,
            "appointment_created": bool(first.calendar_receipt),
            "dedup": second.duplicate and second.outcome_id == first.outcome_id,
            "source_channels": sorted({row["channel"] for row in sources}),
            "uncertain_no_event": uncertain.calendar_receipt is None and uncertain.candidate.uncertain,
            "new_ics_count": len(created),
            "new_ics": created,
        }
        print(json.dumps(payload, sort_keys=True, indent=2))
        memory.close()
    ok = (
        payload["task_persisted"]
        and payload["appointment_created"]
        and payload["dedup"]
        and payload["source_channels"] == ["email", "whatsapp"]
        and payload["uncertain_no_event"]
        and payload["new_ics_count"] == 1
    )
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
