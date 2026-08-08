from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
from typing import Any, Mapping


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def canonical_json(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")


def atomic_write(path: Path, data: bytes, *, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    with temporary.open("wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    temporary.chmod(mode)
    os.replace(temporary, path)


@dataclass(frozen=True)
class Artifact:
    ref: str
    path: Path
    size: int


class ArtifactStore:
    """Content-addressed immutable blobs plus append-only provenance records."""

    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.objects = self.root / "objects"
        self.provenance = self.root / "provenance.jsonl"

    def put(self, data: bytes, *, media_type: str, provenance: Mapping[str, Any]) -> Artifact:
        digest = sha256_bytes(data)
        target = self.objects / digest[:2] / digest
        if target.exists():
            if sha256_bytes(target.read_bytes()) != digest:
                raise RuntimeError("artifact_hash_collision")
        else:
            atomic_write(target, data, mode=0o400)
        record = {
            "v": 1,
            "ref": f"sha256:{digest}",
            "bytes": len(data),
            "media_type": media_type,
            "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "provenance": dict(provenance),
        }
        self._append_provenance(record)
        return Artifact(record["ref"], target, len(data))

    def get(self, ref: str) -> bytes:
        if not ref.startswith("sha256:"):
            raise ValueError("invalid_artifact_ref")
        digest = ref.removeprefix("sha256:")
        if len(digest) != 64:
            raise ValueError("invalid_artifact_ref")
        data = (self.objects / digest[:2] / digest).read_bytes()
        if sha256_bytes(data) != digest:
            raise RuntimeError("artifact_manifest_mismatch")
        return data

    def _append_provenance(self, record: Mapping[str, Any]) -> None:
        self.provenance.parent.mkdir(parents=True, exist_ok=True)
        line = canonical_json(record) + b"\n"
        fd = os.open(self.provenance, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.write(fd, line)
            os.fsync(fd)
        finally:
            os.close(fd)


class VersionedCache:
    REQUIRED_KEY_FIELDS = {
        "input_hash",
        "model_hash",
        "config_hash",
        "schema_version",
        "policy_version",
        "context_hash",
    }

    def __init__(self, root: str | Path):
        self.root = Path(root)

    def key(self, fields: Mapping[str, Any]) -> str:
        if set(fields) != self.REQUIRED_KEY_FIELDS:
            raise ValueError("incomplete_cache_key")
        return sha256_bytes(canonical_json(fields))

    def get(self, key: str) -> Any | None:
        target = self.root / key[:2] / f"{key}.json"
        if not target.exists():
            return None
        envelope = json.loads(target.read_text(encoding="utf-8"))
        if envelope.get("key") != key:
            raise RuntimeError("cache_key_mismatch")
        return envelope.get("value")

    def put(self, key: str, value: Any, *, side_effect: bool = False) -> None:
        if side_effect:
            raise ValueError("side_effect_not_cacheable")
        target = self.root / key[:2] / f"{key}.json"
        atomic_write(target, canonical_json({"key": key, "value": value}) + b"\n")
