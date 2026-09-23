from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import hashlib
import json
import os
from pathlib import Path
from typing import Any, Mapping

from .agenda_ingress import AgendaIntake


@dataclass(frozen=True)
class CallAgendaTriggerResult:
    ready_seen: int
    ingested: int
    skipped: int
    missing_transcript: int


def _parse_timestamp(value: Any, fallback: datetime) -> datetime:
    text = str(value or "").strip()
    if not text:
        return fallback
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return fallback
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


class CallAgendaTrigger:
    """Feed ready local call transcripts into Agenda once per transcript hash."""

    def __init__(
        self,
        *,
        recordings_root: str | Path,
        agenda_intake: AgendaIntake,
        state_path: str | Path,
        now=None,
    ) -> None:
        self.recordings_root = Path(recordings_root).expanduser()
        self.agenda_intake = agenda_intake
        self.state_path = Path(state_path).expanduser()
        self.now = now or (lambda: datetime.now(UTC))

    def poll(self) -> CallAgendaTriggerResult:
        state = self._load_state()
        processed = dict(state.get("processed") or {})
        ready_seen = ingested = skipped = missing_transcript = 0
        metadata_dir = self.recordings_root / "metadata"
        if not metadata_dir.is_dir():
            return CallAgendaTriggerResult(0, 0, 0, 0)

        for metadata_path in sorted(metadata_dir.glob("*.json")):
            try:
                metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            except (OSError, ValueError, json.JSONDecodeError):
                skipped += 1
                continue
            if (
                not isinstance(metadata, Mapping)
                or str(metadata.get("transcription_state") or "") != "ready"
            ):
                continue
            ready_seen += 1
            recording_id = str(metadata.get("recording_id") or "").strip()
            if not recording_id:
                skipped += 1
                continue
            transcript_path = self._safe_transcript_path(metadata)
            if transcript_path is None or not transcript_path.is_file():
                missing_transcript += 1
                continue
            try:
                transcript = " ".join(
                    transcript_path.read_text(encoding="utf-8").split()
                )
            except OSError:
                missing_transcript += 1
                continue
            transcript_hash = hashlib.sha256(transcript.encode("utf-8")).hexdigest()
            previous = processed.get(recording_id)
            if (
                isinstance(previous, Mapping)
                and previous.get("transcript_sha256") == transcript_hash
            ):
                skipped += 1
                continue

            provenance = metadata.get("provenance")
            provenance = provenance if isinstance(provenance, Mapping) else {}
            timestamp = _parse_timestamp(
                provenance.get("call_started_at") or metadata.get("created_at"),
                self.now(),
            )
            caller = str(provenance.get("caller") or "unknown").strip() or "unknown"
            result = None
            if transcript:
                result = self.agenda_intake.ingest_call_transcript(
                    call_id=recording_id,
                    contact=caller,
                    timestamp=timestamp,
                    transcript=transcript,
                )
            processed[recording_id] = {
                "transcript_sha256": transcript_hash,
                "ingested_at": self.now().isoformat(),
                "outcome_id": (
                    getattr(result, "outcome_id", None)
                    if result is not None
                    else None
                ),
                "kind": (
                    getattr(getattr(result, "kind", None), "value", None)
                    if result is not None
                    else None
                ),
            }
            if result is None:
                skipped += 1
            else:
                ingested += 1

        self._save_state(
            {"schema_version": "call_agenda_trigger_v1", "processed": processed}
        )
        return CallAgendaTriggerResult(
            ready_seen, ingested, skipped, missing_transcript
        )

    def _safe_transcript_path(
        self, metadata: Mapping[str, Any]
    ) -> Path | None:
        raw = str(metadata.get("transcript_path") or "").strip()
        if not raw:
            recording_id = str(metadata.get("recording_id") or "").strip()
            raw = str(
                self.recordings_root / "transcripts" / f"{recording_id}.txt"
            )
        path = Path(raw).expanduser()
        try:
            resolved = path.resolve(strict=False)
            root = self.recordings_root.resolve(strict=False)
            resolved.relative_to(root)
        except (OSError, ValueError):
            return None
        return resolved

    def _load_state(self) -> dict[str, Any]:
        try:
            value = json.loads(self.state_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {"schema_version": "call_agenda_trigger_v1", "processed": {}}
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueEError("call_agenda_trigger_state_invalid") from exc
        if not isinstance(value, dict):
            raise ValueError("call_agenda_trigger_state_invalid")
        return value

    def _save_state(self, state: Mapping[str, Any]) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.state_path.with_suffix(self.state_path.suffix + ".tmp")
        temporary.write_text(
            json.dumps(dict(state), ensure_ascii=False, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, self.state_path)


__all__ = ["CallAgendaTrigger", "CallAgendaTriggerResult"]
