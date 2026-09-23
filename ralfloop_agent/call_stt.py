from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import os
from pathlib import Path
from typing import Any, Protocol

from ralfloop_agent.call_recordings import CallRecordingStore


class AudioTranscriber(Protocol):
    def transcribe(self, audio_path: Path) -> dict[str, Any]: ...


@dataclass
class FasterWhisperTranscriber:
    model_name: str
    language: str | None = "it"
    device: str = "auto"
    compute_type: str = "default"
    cpu_threads: int = 0
    local_files_only: bool = True
    download_root: str | None = None

    def __post_init__(self) -> None:
        self._model = None

    @classmethod
    def from_env(cls) -> "FasterWhisperTranscriber":
        language = os.getenv("BOTTAZZI_CALL_STT_LANGUAGE", "it").strip() or None
        cache_root = os.getenv("BOTTAZZI_CALL_STT_CACHE_DIR", "").strip()
        if not cache_root:
            xdg_cache = os.getenv("XDG_CACHE_HOME", "").strip()
            cache_root = str((Path(xdg_cache).expanduser() if xdg_cache else Path.home() / ".cache") / "faster-whisper")
        model_name = os.getenv(
            "BOTTAZZI_CALL_STT_MODEL",
            "mobiuslabsgmbh/faster-whisper-large-v3-turbo",
        ).strip() or "mobiuslabsgmbh/faster-whisper-large-v3-turbo"
        return cls(
            model_name=model_name,
            language=language,
            device=os.getenv("BOTTAZZI_CALL_STT_DEVICE", "auto").strip() or "auto",
            compute_type=os.getenv("BOTTAZZI_CALL_STT_COMPUTE_TYPE", "default").strip() or "default",
            cpu_threads=max(0, int(os.getenv("BOTTAZZI_CALL_STT_CPU_THREADS", "0"))),
            local_files_only=os.getenv("BOTTAZZI_CALL_STT_LOCAL_ONLY", "1").strip().casefold()
            not in {"0", "false", "no", "off"},
            download_root=cache_root,
        )

    def _load_model(self):
        if self._model is None:
            from faster_whisper import WhisperModel

            self._model = WhisperModel(
                self.model_name,
                device=self.device,
                compute_type=self.compute_type,
                cpu_threads=self.cpu_threads,
                local_files_only=self.local_files_only,
                download_root=self.download_root,
            )
        return self._model

    def transcribe(self, audio_path: Path) -> dict[str, Any]:
        segments, info = self._load_model().transcribe(
            str(audio_path),
            language=self.language,
            vad_filter=True,
            beam_size=5,
            condition_on_previous_text=True,
        )
        parts: list[str] = []
        count = 0
        for segment in segments:
            text = " ".join(str(segment.text or "").split())
            if text:
                parts.append(text)
                count += 1
        transcript = " ".join(parts).strip()
        return {
            "text": transcript,
            "language": getattr(info, "language", self.language),
            "language_probability": getattr(info, "language_probability", None),
            "duration_seconds": getattr(info, "duration", None),
            "segments": count,
            "model": self.model_name,
        }


def _atomic_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text.rstrip() + "\n", encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def transcribe_pending(
    store: CallRecordingStore,
    transcriber: AudioTranscriber,
    *,
    limit: int = 10,
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for item in store.list(limit=200):
        if len(results) >= max(1, limit):
            break
        state = str(item.get("transcription_state") or "pending")
        if state not in {"pending", "retry"}:
            continue
        recording_id = str(item["recording_id"])
        audio_path = Path(str(item["audio_path"]))
        transcript_path = Path(str(item["transcript_path"]))
        if not audio_path.is_file():
            results.append(
                store.update_metadata(
                    recording_id,
                    transcription_state="error",
                    transcription_error="audio_missing",
                )
            )
            continue
        try:
            output = transcriber.transcribe(audio_path)
            transcript = str(output.get("text") or "").strip()
            _atomic_text(transcript_path, transcript)
            changes = {
                "transcription_state": "ready",
                "transcribed_at": datetime.now(timezone.utc).isoformat(),
                "transcript_empty": not bool(transcript),
                "stt_language": output.get("language"),
                "stt_language_probability": output.get("language_probability"),
                "stt_duration_seconds": output.get("duration_seconds"),
                "stt_segments": output.get("segments"),
                "stt_model": output.get("model"),
            }
            results.append(store.update_metadata(recording_id, **changes))
        except Exception as exc:
            results.append(
                store.update_metadata(
                    recording_id,
                    transcription_state="retry",
                    transcription_error=exc.__class__.__name__,
                )
            )
    return results
