from __future__ import annotations

import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time

from ralfloop_agent.teacher.service import TeacherService
from ralfloop_agent.teacher.store import TeacherStore
from src.mcp_transport import MCPClientSession, UnixMCPTransport
from src.teacher import ALL_TOOLS, HINT


def fake_model(system_prompt: str, user_prompt: str):
    data = json.loads(user_prompt)
    action = data["action"]

    if action == "check_answer":
        answer = data["request"]["student_answer"].strip()
        return {
            "response": (
                "La risposta è corretta."
                if answer == "4"
                else "Controlla il passaggio precedente senza saltare alla soluzione."
            ),
            "correct": answer == "4",
        }

    if action == "hint":
        return {
            "response": "Prova a isolare prima l'incognita."
        }

    return {
        "response": f"Risposta didattica per {action}."
    }


def test_card_maps_to_stable_student_without_storing_raw_card(tmp_path):
    db = tmp_path / "teacher.sqlite3"
    store = TeacherStore(db)

    first = store.login(
        "TESSERA-123",
        school_level="secondaria I grado",
        class_year="2",
    )
    second = store.login("TESSERA-123")

    assert first["student_id"] == second["student_id"]

    with sqlite3.connect(db) as conn:
        raw = conn.execute(
            "SELECT card_hash FROM students"
        ).fetchone()[0]

    assert raw != "TESSERA-123"
    assert len(raw) == 64


def test_two_students_are_isolated(tmp_path):
    store = TeacherStore(tmp_path / "teacher.sqlite3")

    a = store.login("CARD-A")
    b = store.login("CARD-B")

    assert a["student_id"] != b["student_id"]


def test_vertical_slice_persists_progress(tmp_path):
    store = TeacherStore(tmp_path / "teacher.sqlite3")
    teacher = TeacherService(
        store,
        model_call=fake_model,
    )

    login = teacher.login(
        "CARD-1",
        "primaria",
        "4",
    )
    student_id = login["student"]["student_id"]

    started = teacher.start_session(
        student_id,
        "matematica",
        "addizioni",
    )
    session_id = started["session"]["session_id"]

    explained = teacher.explain(
        session_id,
        "Quanto fa 2 + 2?",
    )
    assert explained["ok"] is True

    exercise = teacher.generate_exercise(
        session_id,
        "addizioni",
    )
    assert exercise["ok"] is True

    checked = teacher.check_answer(
        session_id,
        "Quanto fa 2 + 2?",
        "4",
    )
    assert checked["correct"] is True

    teacher.end_session(session_id)

    relogin = teacher.login("CARD-1")
    assert relogin["student"]["student_id"] == student_id

    progress = teacher.student_progress(student_id)
    assert progress["progress"][0]["attempts"] == 1
    assert progress["progress"][0]["correct"] == 1


def test_hint_does_not_request_solution(tmp_path):
    store = TeacherStore(tmp_path / "teacher.sqlite3")
    captured = {}

    def capture_model(system_prompt, user_prompt):
        captured["payload"] = json.loads(user_prompt)
        return {"response": "Guarda il primo passaggio."}

    teacher = TeacherService(
        store,
        model_call=capture_model,
    )

    student = teacher.login(
        "CARD-H",
        "secondaria II grado",
        "2",
    )["student"]

    session = teacher.start_session(
        student["student_id"],
        "matematica",
        "equazioni",
    )["session"]

    teacher.hint(
        session["session_id"],
        "2x + 5 = 19",
        "2x = 14",
    )

    instruction = captured["payload"]["request"]["instruction"]

    assert "NON fornire la soluzione" in instruction


def test_prepare_reading_is_tts_stub(tmp_path):
    store = TeacherStore(tmp_path / "teacher.sqlite3")
    teacher = TeacherService(
        store,
        model_call=fake_model,
    )

    student = teacher.login("CARD-R")["student"]
    session = teacher.start_session(
        student["student_id"],
        "italiano",
    )["session"]

    out = teacher.prepare_reading(
        session["session_id"],
        "Primo paragrafo.\nSecondo paragrafo.",
        200,
    )

    assert out["tts_status"] == "stub"
    assert out["chunks"]


def test_teacher_broker_lists_strict_tools(tmp_path):
    socket_path = tmp_path / "mcp.sock"
    db_path = tmp_path / "teacher.sqlite3"

    server = Path(
        "scripts/ralf_teacher_mcp_server.py"
    ).resolve()

    broker = subprocess.Popen(
        [
            sys.executable,
            "scripts/ralf_teacher_mcp_broker.py",
            "--socket",
            str(socket_path),
            "--allow-uid",
            str(os.getuid()),
            "--command",
            str(server),
            "--idle-timeout",
            "5",
        ],
        env={
            **os.environ,
            "RALF_TEACHER_DB": str(db_path),
        },
    )

    try:
        deadline = time.monotonic() + 3

        while (
            not socket_path.exists()
            and time.monotonic() < deadline
        ):
            time.sleep(0.02)

        with MCPClientSession(
            UnixMCPTransport(str(socket_path)),
            timeout=3,
        ) as session:
            tools = session.list_tools()

        names = {tool.name for tool in tools}

        assert names == set(ALL_TOOLS)

        hint = next(
            tool
            for tool in tools
            if tool.name == HINT
        )

        assert hint.input_schema["additionalProperties"] is False

    finally:
        broker.terminate()
        broker.wait(timeout=3)


def test_teacher_normalizes_bare_response_prefix():
    from ralfloop_agent.teacher.service import _normalize_teacher_model_result

    result = _normalize_teacher_model_result(
        'response:"Puoi iniziare sottraendo 8 da entrambi i lati."'
    )

    assert result == {
        "response": "Puoi iniziare sottraendo 8 da entrambi i lati."
    }


def test_teacher_normalizes_nested_response_object():
    from ralfloop_agent.teacher.service import _normalize_teacher_model_result

    result = _normalize_teacher_model_result(
        '{"response":{"spiegazione":"Prima isola il termine con x.",'
        '"domanda":"Quale operazione fai per prima?"}}'
    )

    assert result == {
        "response": (
            "Prima isola il termine con x.\n\n"
            "Quale operazione fai per prima?"
        )
    }


def test_teacher_reuses_session_model_lease_and_releases_on_end(tmp_path):
    class LeaseModel:
        def __init__(self):
            self.active = None
            self.starts = 0
            self.calls = 0
            self.releases = 0
            self.closed = False

        def ensure_session(self, session_id):
            if self.active != session_id:
                self.active = session_id
                self.starts += 1

        def __call__(self, system_prompt, user_prompt):
            assert self.active is not None
            self.calls += 1
            return {"response": "ok"}

        def release_session(self, session_id):
            if self.active == session_id:
                self.active = None
                self.releases += 1

        def close(self):
            self.active = None
            self.closed = True

    store = TeacherStore(tmp_path / "teacher.sqlite3")
    model = LeaseModel()
    teacher = TeacherService(store, model_call=model)

    student = teacher.login("CARD-LEASE")["student"]

    session = teacher.start_session(
        student["student_id"],
        "matematica",
        "equazioni",
    )["session"]

    session_id = session["session_id"]

    teacher.explain(session_id, "Prima domanda")
    teacher.hint(session_id, "2x+5=19")

    assert model.starts == 1
    assert model.calls == 2
    assert model.releases == 0

    teacher.end_session(session_id)

    assert model.releases == 1
    assert model.active is None

    teacher.close()
    assert model.closed is True


def test_teacher_releases_model_lease_on_model_error(tmp_path):
    class FailingLeaseModel:
        def __init__(self):
            self.active = None
            self.released = False

        def ensure_session(self, session_id):
            self.active = session_id

        def __call__(self, system_prompt, user_prompt):
            raise RuntimeError("model_failed")

        def release_session(self, session_id):
            if self.active == session_id:
                self.active = None
                self.released = True

    store = TeacherStore(tmp_path / "teacher.sqlite3")
    model = FailingLeaseModel()
    teacher = TeacherService(store, model_call=model)

    student = teacher.login("CARD-FAIL")["student"]
    session = teacher.start_session(
        student["student_id"],
        "matematica",
        "equazioni",
    )["session"]

    import pytest

    with pytest.raises(RuntimeError, match="model_failed"):
        teacher.explain(
            session["session_id"],
            "domanda",
        )

    assert model.released is True
    assert model.active is None
