from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any
from datetime import datetime, timezone

from .security import redact_secrets


MANIFEST_VERSION = "ralf-distributed-inference-v1"


def canonical_json(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def build_manifest(*, environment: dict[str, Any], configuration: dict[str, Any], results: dict[str, Any]) -> dict[str, Any]:
    payload = redact_secrets(
        {
            "manifest_version": MANIFEST_VERSION,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "environment": environment,
            "configuration": configuration,
            "results": results,
        }
    )
    return {**payload, "content_sha256": hashlib.sha256(canonical_json(payload)).hexdigest()}


def write_manifest(path: str | Path, manifest: dict[str, Any]) -> Path:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(redact_secrets(manifest), ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return destination
