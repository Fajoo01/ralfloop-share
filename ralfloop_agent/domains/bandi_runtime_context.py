from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any


DEFAULT_CALLS_ROOT = Path("/home/sibilla-cumana/.local/state/ralf/bandi_knowledge/calls")
DEFAULT_BANDO_REF = "RLD12025048623"
DEFAULT_ROOT = DEFAULT_CALLS_ROOT / DEFAULT_BANDO_REF
BANDO_REF_RE = re.compile(r"^RL[A-Z]\d{8,16}$", re.I)
ALLOWED_FILES = (
    "application_status.json",
    "eligibility.json",
    "organization_profile.json",
    "project_match.json",
)
MAX_FILE_BYTES = 1_000_000


def load_bandi_runtime_context(
    root: Path | None = None, *, bando_ref: str | None = None
) -> dict[str, Any]:
    """Load allowlisted artifacts for one explicit bando; never cross-fallback."""
    if root is not None and bando_ref is not None:
        raise ValueError("bandi_context_root_and_ref_are_mutually_exclusive")
    requested_ref = ""
    if bando_ref is not None:
        requested_ref = str(bando_ref).strip().upper()
        if not BANDO_REF_RE.fullmatch(requested_ref):
            return {
                "schema": "bandi_runtime_context_v1", "sources": {},
                "requested_bando_ref": requested_ref, "resolution": "invalid_reference",
            }
        root = DEFAULT_CALLS_ROOT / requested_ref
        resolution = "reference_specific"
    else:
        root = root or DEFAULT_ROOT
        resolution = "explicit_root" if root != DEFAULT_ROOT else "legacy_default"
    result: dict[str, Any] = {
        "schema": "bandi_runtime_context_v1", "sources": {}, "resolution": resolution,
    }
    if requested_ref:
        result["requested_bando_ref"] = requested_ref
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
