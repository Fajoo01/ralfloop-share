from __future__ import annotations

import json
from pathlib import Path
from typing import Any


DEFAULT_ROOT = Path(
    "/home/sibilla-cumana/.local/state/ralf/bandi_knowledge/calls/RLD12025048623"
)
ALLOWED_FILES = (
    "application_status.json",
    "eligibility.json",
    "organization_profile.json",
    "project_match.json",
)
MAX_FILE_BYTES = 1_000_000


def load_bandi_runtime_context(root: Path = DEFAULT_ROOT) -> dict[str, Any]:
    """Load only fixed, machine-readable Bandi knowledge artifacts."""
    result: dict[str, Any] = {"schema": "bandi_runtime_context_v1", "sources": {}}
    for name in ALLOWED_FILES:
        path = root / name
        if not path.is_file() or path.stat().st_size > MAX_FILE_BYTES:
            result["sources"][name] = {"status": "missing_or_oversized"}
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            result["sources"][name] = {"status": "invalid"}
            continue
        if not isinstance(payload, dict):
            result["sources"][name] = {"status": "invalid"}
            continue
        result[name.removesuffix(".json")] = payload
        result["sources"][name] = {"status": "loaded"}
    return result
