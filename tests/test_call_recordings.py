from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from openshell_backend import assistant_v1_api
from ralfloop_agent.call_recordings import CallRecordingStore
from ralfloop_agent.call_stt import transcribe_pending


def test_call_recording_store_is_content_addressed_and_idempotent(tmp_path: Path) -> None:
    store = CallRecordingStore(tmp_path, max_bytes=1024 * 1024)
    first = store.ingest(
        b"fake-wav-audio",
        filename="chiamata test.wav",
        content_type="audio/wav",
        extra={"transport": "android_share"},
    )
    second = store.ingest(
        b"fake-wav-audio",
        filename="duplicato.wav",
        content_type="audio/wav",
    )

    assert first["recording_id"] == second["recording_id"]
    assert first["duplicate"] is False
    assert second["duplicate"] is True
    assert Path(first["audio_path"]).read_bytes() == b"fake-wav-audio"
    assert first["transcription_state"] == "pending"
    assert first["provenance"]["transport"] == "android_share"
    assert len(store.list()) == 1


def test_call_recording_api_accepts_raw_audio_and_lists_it(tmp_path: Path) -> None:
    store = CallRecordingStore(tmp_path, max_bytes=1024 * 1024)
    app = FastAPI()
    app.include_router(assistant_v1_api.router)
    app.dependency_overrides[assistant_v1_api.get_call_recording_store] = lambda: store
    client = TestClient(app)

    response = client.post(
        "/assistant/v1/call-recordings",
        content=b"audio-from-phone",
        headers={
            "content-type": "audio/mp4",
            "x-bottazzi-filename": "Chiamata+Mario.m4a",
            "x-bottazzi-transport": "android_share",
        },
    )
    assert response.status_code == 201
    recording = response.json()["recording"]
    assert recording["filename"] == "Chiamata Mario.m4a"
    assert recording["content_type"] == "audio/mp4"
    assert recording["provenance"]["transport"] == "android_share"

    listing = client.get("/assistant/v1/call-recordings")
    assert listing.status_code == 200
    rows = listing.json()["recordings"]
    assert len(rows) == 1
    assert rows[0]["recording_id"] == recording["recording_id"]


def test_call_recording_api_rejects_non_audio(tmp_path: Path) -> None:
    store = CallRecordingStore(tmp_path, max_bytes=1024 * 1024)
    app = FastAPI()
    app.include_router(assistant_v1_api.router)
    app.dependency_overrides[assistant_v1_api.get_call_recording_store] = lambda: store
    client = TestClient(app)

    response = client.post(
        "/assistant/v1/call-recordings",
        content=b"not-a-recording",
        headers={"content-type": "text/plain"},
    )
    assert response.status_code == 400
    assert response.json()["detail"] == "unsupported_recording_type"


class _FakeTranscriber:
    def transcribe(self, audio_path: Path):
        assert audio_path.is_file()
        return {
            "text": "Ci vediamo giovedì alle diciotto.",
            "language": "it",
            "language_probability": 0.99,
            "duration_seconds": 12.5,
            "segments": 1,
            "model": "fake-local",
        }


def test_pending_recording_is_transcribed_and_metadata_becomes_ready(tmp_path: Path) -> None:
    store = CallRecordingStore(tmp_path, max_bytes=1024 * 1024)
    recording = store.ingest(
        b"fake-audio",
        filename="telefonata.m4a",
        content_type="audio/mp4",
    )

    processed = transcribe_pending(store, _FakeTranscriber(), limit=1)

    assert len(processed) == 1
    row = processed[0]
    assert row["recording_id"] == recording["recording_id"]
    assert row["transcription_state"] == "ready"
    assert row["stt_model"] == "fake-local"
    assert Path(row["transcript_path"]).read_text(encoding="utf-8").strip() == (
        "Ci vediamo giovedì alle diciotto."
    )


def test_ready_recording_is_not_transcribed_twice(tmp_path: Path) -> None:
    store = CallRecordingStore(tmp_path, max_bytes=1024 * 1024)
    store.ingest(b"audio", filename="telefonata.ogg", content_type="audio/ogg")
    transcriber = _FakeTranscriber()
    assert len(transcribe_pending(store, transcriber, limit=1)) == 1
    assert transcribe_pending(store, transcriber, limit=1) == []
