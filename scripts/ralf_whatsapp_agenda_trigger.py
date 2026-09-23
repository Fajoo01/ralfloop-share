#!/usr/bin/env python3
from __future__ import annotations

import json
import os
from pathlib import Path

from src.whatsapp import WhatsAppMCPContext
from ralfloop_agent.unified_assistant.agenda_ingress import build_default_agenda_intake
from ralfloop_agent.unified_assistant.memory_service import MemoryService
from ralfloop_agent.unified_assistant.whatsapp_agenda_trigger import WhatsAppAgendaTrigger


def main() -> int:
    memory_path = Path(os.getenv(
        "RALFLOOP_OPERATIONAL_MEMORY_PATH",
        str(Path.home() / ".local/share/bottazzi/runtime-production/operational-memory.sqlite3"),
    ))
    state_path = Path(os.getenv(
        "BOTTAZZI_WHATSAPP_AGENDA_STATE",
        str(Path.home() / ".local/state/ralf/whatsapp-agenda-trigger.json"),
    ))
    with MemoryService(memory_path) as memory:
        intake = build_default_agenda_intake(memory)
        trigger = WhatsAppAgendaTrigger(
            gateway_factory=WhatsAppMCPContext.from_environment,
            agenda_intake=intake,
            state_path=state_path,
        )
        print(json.dumps(trigger.poll().__dict__, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
