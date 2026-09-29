from datetime import UTC, datetime
import json
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from ralfloop_agent.unified_assistant.agenda import (
    AgendaKind,
    AgendaPipeline,
    AgendaSource,
    AgendaStore,
    LocalIcsCalendarProvider,
)
from ralfloop_agent.unified_assistant.call_agenda_trigger import (
    CallAgendaHttpIntake,
    CallAgendaTrigger,
)
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


def test_call_http_intake_is_loopback_only_and_preserves_payload(tmp_path: Path) -> None:
    pipeline, _, _, _ = _pipeline(tmp_path)
    expected = pipeline.process(_source("phone_call", "call-http-result", "Ricordami di chiamare Marco"))

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"ok": True, "agenda": expected.model_dump(mode="json")}

    class FakeSession:
        def __init__(self):
            self.calls = []

        def post(self, url, *, json, timeout):
            self.calls.append((url, json, timeout))
            return FakeResponse()

    session = FakeSession()
    intake = CallAgendaHttpIntake(
        "http://127.0.0.1:19090/assistant/v1/agenda/ingest",
        timeout_sec=3,
        session=session,
    )
    timestamp = datetime(2026, 9, 23, 8, 0, tzinfo=ROME)
    result = intake.ingest_call_transcript(
        call_id="call-http-1",
        contact="+393331234567",
        timestamp=timestamp,
        transcript="Ricordami di chiamare Marco",
    )

    assert result == expected
    assert session.calls == [(
        "http://127.0.0.1:19090/assistant/v1/agenda/ingest",
        {
            "channel": "phone_call",
            "sender": "+393331234567",
            "native_id": "call-http-1",
            "timestamp": timestamp.isoformat(),
            "original_text": "Ricordami di chiamare Marco",
        },
        3.0,
    )]
    with pytest.raises(ValueError, match="loopback"):
        CallAgendaHttpIntake("https://example.com/assistant/v1/agenda/ingest")


def test_call_agenda_trigger_rejects_corrupt_state(tmp_path: Path) -> None:
    state = tmp_path / "state.json"
    state.write_text("{broken", encoding="utf-8")
    trigger = CallAgendaTrigger(
        recordings_root=tmp_path / "recordings",
        agenda_intake=SimpleNamespace(),
        state_path=state,
    )
    with pytest.raises(ValueError, match="call_agenda_trigger_state_invalid"):
        trigger.poll()


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


def test_explicit_date_rich_clock_and_unknown_sender_title(tmp_path: Path) -> None:
    pipeline, _, _, _ = _pipeline(tmp_path)
    result = pipeline.process(_source(
        "phone_call", "call-date-1",
        "Riunione con Marco il 29 settembre verso le 18.30",
        sender="unknown",
    ))
    assert result.kind is AgendaKind.APPOINTMENT
    assert result.candidate.title == "Incontro con Marco"
    assert result.candidate.start_at == datetime(2026, 9, 29, 18, 30, tzinfo=ROME)


def test_word_clock_and_evening_period_are_understood(tmp_path: Path) -> None:
    pipeline, _, _, _ = _pipeline(tmp_path)
    result = pipeline.process(_source(
        "whatsapp", "wa-word-time",
        "Ci sentiamo domani alle sei e mezza di sera",
        sender="Luca",
    ))
    assert result.kind is AgendaKind.APPOINTMENT
    assert result.candidate.start_at == datetime(2026, 9, 24, 18, 30, tzinfo=ROME)


def test_meeting_range_sets_real_end_time(tmp_path: Path) -> None:
    pipeline, _, _, _ = _pipeline(tmp_path)
    result = pipeline.process(_source("email", "mail-range", "Riunione domani dalle 18 alle 19:30", sender="Luca"))
    assert result.candidate.start_at == datetime(2026, 9, 24, 18, 0, tzinfo=ROME)
    assert result.candidate.end_at == datetime(2026, 9, 24, 19, 30, tzinfo=ROME)


def test_cross_channel_wording_variants_merge_by_person_and_time(tmp_path: Path) -> None:
    pipeline, _, store, calendar_dir = _pipeline(tmp_path)
    first = pipeline.process(_source(
        "email", "mail-irene", "Appuntamento giovedì alle 18", sender="Irene Conca",
    ))
    second = pipeline.process(_source(
        "whatsapp", "wa-irene", "Ci sentiamo giovedì ore 18", sender="Irene Conca",
    ))
    assert first.outcome_id == second.outcome_id
    assert second.duplicate is True
    assert len(list(calendar_dir.glob("*.ics"))) == 1
    assert {row["channel"] for row in store.sources(first.dedup_key)} == {"email", "whatsapp"}


def test_calendar_side_effect_is_idempotent_even_if_store_is_lost(tmp_path: Path) -> None:
    calendar_dir = tmp_path / "calendar"
    source = _source("phone_call", "same-call", "Ci vediamo domani alle 10", sender="Luca")
    first, _, _, _ = _pipeline(tmp_path / "one")
    first.calendar = LocalIcsCalendarProvider(calendar_dir)
    a = first.process(source)
    second, _, _, _ = _pipeline(tmp_path / "two")
    second.calendar = LocalIcsCalendarProvider(calendar_dir)
    b = second.process(source)
    assert a.outcome_id == b.outcome_id
    assert len(list(calendar_dir.glob("*.ics"))) == 1


def test_long_email_information_uses_bounded_semantic_key(tmp_path: Path) -> None:
    pipeline, _, _, _ = _pipeline(tmp_path)
    text = "Newsletter informativa " + ("contenuto senza appuntamenti " * 300)
    result = pipeline.process(_source("email", "mail-long", text, sender="Newsletter"))
    assert result.kind is AgendaKind.INFORMATION
    assert result.candidate.semantic_key.startswith("information|sha256:")
    assert len(result.candidate.semantic_key) < 1000


def test_same_phone_call_keeps_one_calendar_file_if_transcript_grows(tmp_path: Path) -> None:
    pipeline, _, _, calendar_dir = _pipeline(tmp_path)
    first = pipeline.process(_source(
        "phone_call", "call-stable-1", "Ci vediamo domani alle 10", sender="unknown",
    ))
    second = pipeline.process(_source(
        "phone_call", "call-stable-1", "Ci vediamo domani alle 10 per parlare del progetto", sender="unknown",
    ))
    assert first.kind is AgendaKind.APPOINTMENT
    assert second.kind is AgendaKind.APPOINTMENT
    assert len(list(calendar_dir.glob("*.ics"))) == 1


def test_email_rivediamo_binds_date_and_time_to_same_clause(tmp_path: Path) -> None:
    pipeline, _, _, _ = _pipeline(tmp_path)
    text = (
        "Grazie per l'incontro di stasera!\n"
        "Compilare il file entro mercoledì 30.\n"
        "QUI la cartella con tutto.\n"
        "Ci rivediamo giovedì 8/10 alle 18.30 sempre on-line, resto a disposizione."
    )
    results = pipeline.process_all(_source("email", "mail-irene", text, sender="Irene Conca"))
    appointments = [r for r in results if r.kind is AgendaKind.APPOINTMENT]
    assert len(appointments) == 1
    assert appointments[0].candidate.title == "Incontro con Irene Conca"
    assert appointments[0].candidate.start_at == datetime(2026, 10, 8, 18, 30, tzinfo=ROME)
