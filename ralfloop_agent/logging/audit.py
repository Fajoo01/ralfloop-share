from __future__ import annotations

import json
from datetime import datetime, UTC
from pathlib import Path
from typing import Any


class AuditLogger:
    def __init__(self, store_path: str = "./logs") -> None:
        self.store = Path(store_path)
        self.store.mkdir(parents=True, exist_ok=True)
        self.logfile = self.store / "audit.jsonl"

    def log(self, **payload: Any) -> None:
        record = {"timestamp": datetime.now(UTC).isoformat(), **payload}
        with self.logfile.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
