from datetime import UTC, datetime
import json
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from ralfloop_agent.unified_assistant.agenda import (
    AgendaKind,
    AgendaPipeline,
    AgendaSource,
    AgendaStore,
    LocalIcsCalendarProvider,
)
from ralfloop_agent.unified_assistant.call_agenda_trigger import CallAgendaTrigger
from ralfloop_agent.unified_assistant.memory_service import MemoryService
from ralfloop_agent.unified_assistant.task_queue import BotTazziTaskQueue, JevPriorityClassifier


ROME = ZoneInfo("Europe/Rome")
NOW = datetime(2026, 9, 23, 7, 10, tzinfo=ROME)


def _pipeline(tmp_path: Path) -> tuple[AgendaPipeline, BotTazziTaskQueue, AgendaStore, Path]:
    calendar_dir = tmp_path / "calendar"
    queue = BotTazziTaskQueue(
        tmp_path / "tasks.sqlite3",
        classifier=JevPriorityClassifier(base_url="http://127.0.0.1:9", timeout_sec=0.05),
    )
    store = AgendaStore(tmp_path / "agenda.sqlite3")
    memory = MemoryService(tmp_path / "memory.sqlite3")
    pipeline = AgendaPipeline(
        store=store,
        queue=queue,
        memory=memory,
        calendar=LocalIcsCalendarProvider(calendar_dir),
    )
    return pipeline, queue, store, calendar_dir


def _source(channel: str, native_id: str, text: str, *, sender: str = "Luca") -> AgendaSource:
    return AgendaSource(
        channel=channel,
        sender=sender,
        native_id=native_id,
        timestamp=NOW,
        original_text=text,
    )


def test_certain_event_is_created(tmp_path: Path) -> None:
    pipeline, _, _, calendar_dir = _pipeline(tmp_path)
    result = pipeline.process(_source("whatsapp", "wa-1", "Ci vediamo giovedì alle 18"))
    assert result.kind is AgendaKind.APPOINTMENT
    assert result.duplicate is False
    assert result.candidate.start_at == datetime(2026, 9, 24, 18, 0, tzinfo=ROME)
    files = list(calendar_dir.glob("*.ics"))
    assert len(files) == 1
    assert "DTSTART:20260924T160000Z" in files[0].read_text(encoding="utf-8")


def test_ambiguous_event_never_auto_creates(tmp_path: Path) -> None:
    pipeline, _, _, calendar_dir = _pipeline(tmp_path)
    result = pipeline.process(_source("email", "mail-1", "Forse ci vediamo giovedì alle 18"))
    assert result.kind is AgendaKind.INFORMATION
    assert result.candidate.uncertain is True
    assert result.candidate.needs_motor is True
    assert list(calendar_dir.glob("*.ics")) == []


def test_task_with_deadline_goes_through_existing_jev_queue(tmp_path: Path) -> None:
    pipeline, queue, _, _ = _pipeline(tmp_path)
    result = pipeline.process(_source("email", "mail-task-1", "Ricordami di chiamare Luca domani", sender="Fabio"))
    assert result.kind is AgendaKind.TASK
    task = queue.get_task(result.outcome_id or "")
    assert task.title == "chiamare Luca"
    assert datetime.fromtimestamp(task.deadline_epoch or 0, tz=ROME) == datetime(2026, 9, 24, 23, 59, tzinfo=ROME)
    assert task.classifier_source in {"jev_typed_local", "jev_unavailable_fallback"}


def test_cross_channel_dedup_keeps_both_sources(tmp_path: Path) -> None:
    pipeline, _, store, calendar_dir = _pipeline(tmp_path)
    first = pipeline.process(_source("email", "mail-event-1", "Ci vediamo giovedì alle 18"))
    second = pipeline.process(_source("whatsapp", "wa-event-1", "Ci vediamo giovedì alle 18"))
    assert first.outcome_id == second.outcome_id
    assert second.duplicate is True
    assert len(list(calendar_dir.glob("*.ics"))) == 1
    sources = store.sources(first.dedup_key)
    assert {row["channel"] for row in sources} == {"email", "whatsapp"}
    assert {row["native_id"] for row in sources} == {"mail-event-1", "wa-event-1"}


def test_transcribed_phone_call_uses_same_extractor(tmp_path: Path) -> None:
    pipeline, queue, _, _ = _pipeline(tmp_path)

    class FakeLocalSTT:
        def transcribe(self, content: bytes, mime_type: str) -> str:
            assert content == b"audio"
            assert mime_type == "audio/wav"
            return "Ricordami di chiamare Luca domani"

    result = pipeline.process_call_audio(
        b"audio",
        mime_type="audio/wav",
        sender="Luca",
        call_id="call-123",
        timestamp=NOW,
        stt=FakeLocalSTT(),
    )
    assert result.kind is AgendaKind.TASK
    task = queue.get_task(result.outcome_id or "")
    assert "source=phone_call" in task.description
    assert "native_id=call-123" in task.description


def test_human_jev_override_survives_new_and_duplicate_intake(tmp_path: Path) -> None:
    pipeline, queue, _, _ = _pipeline(tmp_path)
    first = pipeline.process(_source("email", "mail-low", "Ricordami di comprare il pane domani", sender="Fabio"))
    queue.pin(first.outcome_id or "", 1)
    high = pipeline.process(_source("email", "mail-high", "Ricordami di pagare la TARI domani", sender="Fabio"))
    assert high.kind is AgendaKind.TASK
    assert queue.get_task(first.outcome_id or "").human_rank == 1
    assert queue.list_queue()[0].task.task_id == first.outcome_id

    duplicate = pipeline.process(_source("whatsapp", "wa-low", "Ricordami di comprare il pane domani", sender="Fabio"))
    assert duplicate.duplicate is True
    assert duplicate.outcome_id == first.outcome_id
    assert queue.get_task(first.outcome_id or "").human_rank == 1
    assert queue.list_queue()[0].task.task_id == first.outcome_id


def test_notification_uses_existing_jev_queue_with_deadline(tmp_path: Path) -> None:
    pipeline, queue, _, _ = _pipeline(tmp_path)
    result = pipeline.process(_source("whatsapp", "wa-notify-1", "Avvisami domani alle 18 di chiamare Luca", sender="Fabio"))
    assert result.kind is AgendaKind.NOTIFICATION
    task = queue.get_task(result.outcome_id or "")
    assert datetime.fromtimestamp(task.deadline_epoch or 0, tz=ROME) == datetime(2026, 9, 24, 18, 0, tzinfo=ROME)
    assert "Bot-tazzi Agenda notification" in task.description
    assert task.classifier_source in {"jev_typed_local", "jev_unavailable_fallback"}


def test_motor_is_only_consulted_for_ambiguous_inputs(tmp_path: Path) -> None:
    pipeline, _, _, calendar_dir = _pipeline(tmp_path)

    class CountingResolver:
        calls = 0

        def resolve(self, source, candidate):
            self.calls += 1
            return candidate.model_copy(update={"needs_motor": False})

    resolver = CountingResolver()
    pipeline.ambiguity_resolver = resolver
    pipeline.process(_source("email", "mail-fast", "Ricordami di chiamare Luca domani"))
    assert resolver.calls == 0

    result = pipeline.process(_source("email", "mail-amb", "Forse ci vediamo giovedì alle 18"))
    assert resolver.calls == 1
    assert result.kind is AgendaKind.INFORMATION
    assert list(calendar_dir.glob("*.ics")) == []


def test_utc_transport_timestamp_keeps_spoken_clock_time_in_rome(tmp_path: Path) -> None:
    pipeline, _, _, calendar_dir = _pipeline(tmp_path)
    source = AgendaSource(
        channel="phone_call",
        sender="unknown",
        native_id="call-utc",
        timestamp=datetime(2026, 9, 23, 6, 36, tzinfo=UTC),
        original_text="Ci vediamo domani alle 10",
    )
    result = pipeline.process(source)

    assert result.candidate.start_at == datetime(2026, 9, 24, 10, 0, tzinfo=ROME)
    ics = next(calendar_dir.glob("*.ics")).read_text(encoding="utf-8")
    assert "DTSTART:20260924T080000Z" in ics


def test_single_call_transcript_can_create_both_appointment_and_task(tmp_path: Path) -> None:
    pipeline, queue, store, calendar_dir = _pipeline(tmp_path)
    source = _source(
        "phone_call",
        "call-real-shape",
        "Ci vediamo domani alle 10. Ricordami di chiamare Marco.",
        sender="unknown",
    )
    results = pipeline.process_all(source)

    assert [result.kind for result in results] == [AgendaKind.APPOINTMENT, AgendaKind.TASK]
    assert len(list(calendar_dir.glob("*.ics"))) == 1
    task = queue.get_task(results[1].outcome_id or "")
    assert task.title == "chiamare Marco"
    for result in results:
        sources = store.sources(result.dedup_key)
        assert len(sources) == 1
        assert sources[0]["native_id"] == "call-real-shape"
        assert sources[0]["original_text"] == source.original_text


def test_ready_call_transcript_bridge_preserves_call_provenance_and_is_idempotent(tmp_path: Path) -> None:
    recordings = tmp_path / "recordings"
    metadata_dir = recordings / "metadata"
    transcript_dir = recordings / "transcripts"
    metadata_dir.mkdir(parents=True)
    transcript_dir.mkdir(parents=True)
    recording_id = "a" * 64
    transcript_path = transcript_dir / f"{recording_id}.txt"
    transcript_path.write_text("Ricordami di chiamare Luca domani\n", encoding="utf-8")
    (metadata_dir / f"{recording_id}.json").write_text(json.dumps({
        "recording_id": recording_id,
        "transcription_state": "ready",
        "created_at": "2026-09-23T05:00:00+00:00",
        "transcript_path": str(transcript_path),
        "provenance": {
            "caller": "+393331234567",
            "call_started_at": "2026-09-23T07:05:00+02:00",
        },
    }), encoding="utf-8")

    class FakeIntake:
        def __init__(self):
            self.calls = []

        def ingest_call_transcript(self, **kwargs):
            self.calls.append(kwargs)
            return SimpleNamespace(outcome_id="task-call-1", kind=AgendaKind.TASK)

    intake = FakeIntake()
    trigger = CallAgendaTrigger(
        recordings_root=recordings,
        agenda_intake=intake,
        state_path=tmp_path / "call-agenda-state.json",
        now=lambda: NOW,
    )
    first = trigger.poll()
    second = trigger.poll()

    assert first.ingested == 1
    assert second.ingested == 0
    assert second.skipped == 1
    assert intake.calls == [{
        "call_id": recording_id,
        "contact": "+393331234567",
        "timestamp": datetime(2026, 9, 23, 7, 5, tzinfo=ROME),
        "transcript": "Ricordami di chiamare Luca domani",
    }]


def test_plain_information_is_searchable_in_rag(tmp_path: Path) -> None:
    pipeline, _, _, _ = _pipeline(tmp_path)
    result = pipeline.process(_source("pec", "pec-info-1", "La pratica TARI è stata protocollata oggi", sender="Difensore regionale"))
    assert result.kind is AgendaKind.INFORMATION
    docs = pipeline.memory.search_documents("pratica TARI")
    assert len(docs) == 1
    assert docs[0].body == "La pratica TARI è stata protocollata oggi"
