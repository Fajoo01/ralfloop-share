from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
from typing import Any


_SAFE_EXT_RE = re.compile(r"^[a-z0-9]{1,8}$")


def _xdg_data_home() -> Path:
    configured = os.getenv("XDG_DATA_HOME", "").strip()
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".local" / "share"


def default_call_recordings_dir() -> Path:
    configured = os.getenv("BOTTAZZI_CALL_RECORDINGS_DIR", "").strip()
    if configured:
        return Path(configured).expanduser()
    return _xdg_data_home() / "bottazzi" / "call-recordings"


def _safe_extension(filename: str, content_type: str) -> str:
    suffix = Path(filename).suffix.lower().lstrip(".")
    if suffix and _SAFE_EXT_RE.fullmatch(suffix):
        return suffix
    common = {
        "audio/wav": "wav",
        "audio/x-wav": "wav",
        "audio/mpeg": "mp3",
        "audio/mp4": "m4a",
        "audio/aac": "aac",
        "audio/ogg": "ogg",
        "audio/webm": "webm",
        "video/mp4": "mp4",
    }
    return common.get(content_type.split(";", 1)[0].strip().lower(), "bin")


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)


@dataclass(frozen=True)
class CallRecordingStore:
    root: Path
    max_bytes: int = 256 * 1024 * 1024

    @classmethod
    def from_env(cls) -> "CallRecordingStore":
        max_mb = int(os.getenv("BOTTAZZI_CALL_RECORDING_MAX_MB", "256"))
        return cls(default_call_recordings_dir(), max_bytes=max(1, max_mb) * 1024 * 1024)

    @property
    def audio_dir(self) -> Path:
        return self.root / "audio"

    @property
    def metadata_dir(self) -> Path:
        return self.root / "metadata"

    @property
    def transcript_dir(self) -> Path:
        return self.root / "transcripts"

    def ensure(self) -> None:
        for path in (self.audio_dir, self.metadata_dir, self.transcript_dir):
            path.mkdir(parents=True, exist_ok=True, mode=0o700)

    def ingest(
        self,
        content: bytes,
        *,
        filename: str,
        content_type: str,
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if not content:
            raise ValueError("empty_recording")
        if len(content) > self.max_bytes:
            raise ValueError("recording_too_large")
        mime = content_type.split(";", 1)[0].strip().lower()
        if not (mime.startswith("audio/") or mime in {"application/octet-stream", "video/mp4"}):
            raise ValueError("unsupported_recording_type")

        self.ensure()
        digest = hashlib.sha256(content).hexdigest()
        extension = _safe_extension(filename, mime)
        audio_path = self.audio_dir / f"{digest}.{extension}"
        metadata_path = self.metadata_dir / f"{digest}.json"
        transcript_path = self.transcript_dir / f"{digest}.txt"
        duplicate = metadata_path.exists()

        if not audio_path.exists():
            tmp = audio_path.with_suffix(audio_path.suffix + ".tmp")
            tmp.write_bytes(content)
            os.chmod(tmp, 0o600)
            os.replace(tmp, audio_path)

        if duplicate:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            metadata["duplicate"] = True
            return metadata

        now = datetime.now(timezone.utc).isoformat()
        metadata: dict[str, Any] = {
            "recording_id": digest,
            "source_type": "phone_call_recording",
            "filename": Path(filename or f"call-recording.{extension}").name,
            "content_type": mime or "application/octet-stream",
            "size_bytes": len(content),
            "sha256": digest,
            "created_at": now,
            "audio_path": str(audio_path),
            "transcript_path": str(transcript_path),
            "transcription_state": "pending",
            "duplicate": False,
        }
        if extra:
            metadata["provenance"] = {k: v for k, v in extra.items() if v not in (None, "")}
        _atomic_json(metadata_path, metadata)
        return metadata

    def list(self, *, limit: int = 50) -> list[dict[str, Any]]:
        self.ensure()
        items: list[dict[str, Any]] = []
        for path in sorted(self.metadata_dir.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
            try:
                item = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError, json.JSONDecodeError):
                continue
            item["transcript_ready"] = Path(str(item.get("transcript_path") or "")).is_file()
            items.append(item)
            if len(items) >= max(1, limit):
                break
        return items

    def metadata_path(self, recording_id: str) -> Path:
        if not re.fullmatch(r"[a-f0-9]{64}", recording_id):
            raise ValueError("invalid_recording_id")
        return self.metadata_dir / f"{recording_id}.json"

    def update_metadata(self, recording_id: str, **changes: Any) -> dict[str, Any]:
        path = self.metadata_path(recording_id)
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload.update(changes)
        _atomic_json(path, payload)
        return payload
