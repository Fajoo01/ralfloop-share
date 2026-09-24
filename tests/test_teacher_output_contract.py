from types import SimpleNamespace

from ralfloop_agent.teacher.pedagogy import SessionMode
from ralfloop_agent.teacher.service import TeacherService, _enforce_model_output_contract
from ralfloop_agent.teacher.store import TeacherStore


def decision(*, limit=180, micro=True, allow=False, mode=SessionMode.STANDARD):
    return SimpleNamespace(
        max_response_chars=limit,
        micro_check=micro,
        allow_final_solution=allow,
        mode=mode,
    )


def test_output_contract_enforces_hard_length_and_keeps_micro_check():
    text = (
        "Questa è una spiegazione volutamente molto lunga che ripete diversi dettagli "
        "per simulare un modello locale troppo verboso. " * 4
    )
    response, guard = _enforce_model_output_contract(
        text,
        decision=decision(limit=160),
        action="explain",
        payload={"question": "Spiegamelo."},
        student_move="question",
        concept_evidence=None,
    )
    assert len(response) <= 160
    assert response.endswith("?")
    assert "length_enforced" in guard["reasons"]
    assert "micro_check_added" in guard["reasons"]


def test_output_contract_preserves_existing_question_when_trimming():
    text = (
        "La corrente in un circuito in serie resta la stessa nei diversi punti. "
        "La lampadina trasforma energia elettrica in luce e calore. "
        "Questo dettaglio serve a distinguere corrente ed energia. "
        "Che cosa resta uguale prima e dopo la lampadina?"
    )
    response, guard = _enforce_model_output_contract(
        text,
        decision=decision(limit=170),
        action="explain",
        payload={"question": "La lampadina consuma corrente?"},
        student_move="claim_check",
        concept_evidence=None,
    )
    assert len(response) <= 170
    assert response.endswith("Che cosa resta uguale prima e dopo la lampadina?")
    assert "micro_check_added" not in guard["reasons"]


def test_output_contract_removes_explicit_final_answer_from_hint():
    response, guard = _enforce_model_output_contract(
        "Scomponi 7 x 8 in 7 x 4 + 7 x 4. 7 x 8 = 56. Ora prova tu.",
        decision=decision(limit=220, micro=False, allow=False),
        action="hint",
        payload={"exercise": "Calcola 7 x 8", "student_attempt": "Non so"},
        student_move="confusion",
        concept_evidence=None,
    )
    assert "56" not in response
    assert "final_answer_removed" in guard["reasons"]


def test_output_contract_replaces_unanchored_model_text_with_curated_evidence():
    evidence = {
        "evidence": (
            "Le piante producono zuccheri usando luce, acqua e anidride carbonica; "
            "le radici assorbono acqua e sali minerali."
        )
    }
    response, guard = _enforce_model_output_contract(
        "Le piante prendono il cibo già pronto dal terreno.",
        decision=decision(limit=260, micro=True),
        action="explain",
        payload={"question": "Le piante mangiano dal terreno?"},
        student_move="claim_check",
        concept_evidence=evidence,
    )
    low = response.casefold()
    assert "luce" in low or "anidride" in low
    assert "cibo già pronto" not in low
    assert "concept_evidence_pinned" in guard["reasons"]


def test_stream_buffers_raw_model_tokens_until_output_guard(tmp_path):
    class Model:
        def __call__(self, *_):
            return {"response": "Il risultato finale è 56."}

        def stream(self, *_):
            yield {"type": "delta", "text": "Il risultato finale è 56."}
            yield {"type": "done", "result": {"response": "Il risultato finale è 56."}}

    service = TeacherService(
        TeacherStore(tmp_path / "stream-guard.sqlite3"),
        model_call=Model(),
    )
    student = service.login("STREAM-GUARD", "middle", "2")["student"]
    session = service.start_session(student["student_id"], "matematica", "tema libero")["session"]
    events = list(service.stream_hint(session["session_id"], "Calcola 7 x 8", "Non so"))

    assert [event["type"] for event in events] == ["delta", "done"]
    assert "56" not in events[0]["text"]
    assert "56" not in events[-1]["result"]["response"]
    assert "final_answer_removed" in events[-1]["result"]["output_guard"]["reasons"]
