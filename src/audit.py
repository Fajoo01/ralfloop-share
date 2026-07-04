from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any
import json
import os


AUDIT_PATH = Path(os.environ.get("RALF_AUDIT_PATH", "logs/audit.jsonl"))


def log_operation(operation_type: str, data: dict[str, Any], audit_path: str | Path | None = None) -> Path:
    path = Path(audit_path) if audit_path is not None else AUDIT_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "timestamp": datetime.now().isoformat(),
        "operation": operation_type,
        **data,
    }
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, ensure_ascii=False, default=str, sort_keys=True) + "\n")
    return path
