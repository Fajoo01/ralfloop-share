from pathlib import Path
import os
import signal
import subprocess
import time

from ralfloop_agent.teacher.service import TeacherService
from ralfloop_agent.teacher.store import TeacherStore
from src.mcp_transport import MCPClientSession, StdioMCPTransport


ROOT = Path(__file__).resolve().parents[1]
CORE_DIR = ROOT / "tools" / "teacher_core_mcp"
BINARY = CORE_DIR / "ralf-teacher-core-mcp"
CONCEPTS = ROOT / "ralfloop_agent" / "teacher" / "data" / "concept_evidence.tsv"


def _session():
    subprocess.run(["make", "-C", str(CORE_DIR)], check=True, capture_output=True)
    return MCPClientSession(
        StdioMCPTransport([str(BINARY), "--stdio", "--concepts", str(CONCEPTS)]),
        timeout=3,
        client_name="teacher-core-test",
    )


def _payload(raw):
    value = raw.get("structuredContent")
    assert isinstance(value, dict)
    assert value["ok"] is True
    assert value["writes"] == 0
    assert value["external_side_effects"] == 0
    return value


def test_core_mcp_real_tools_are_bounded_and_read_only():
    with _session() as session:
        names = {tool.name for tool in session.list_tools()}
        assert names == {
            "core.text_profile",
            "core.extractive_summary",
            "core.study_plan",
            "core.math_check",
            "core.math_hint",
            "core.classify_turn",
            "core.concept_evidence",
        }
        math = _payload(session.call_tool(
            "core.math_check",
            {"text": "3/4 + 1/4", "answer": "1"},
        ))
        assert math["recognized"] is True
        assert math["answer_recognized"] is True
        assert math["equivalent"] is True
        assert math["expected"] == 1

        wrong_equation = _payload(session.call_tool(
            "core.math_check",
            {"text": "Risolvi 2x + 6 = 20", "answer": "x=8"},
        ))
        assert wrong_equation["recognized"] is True
        assert wrong_equation["kind"] == "linear_equation"
        assert wrong_equation["expected"] == 7
        assert wrong_equation["equivalent"] is False

        right_equation = _payload(session.call_tool(
            "core.math_check",
            {"text": "Risolvi 2x + 6 = 20", "answer": "x=7"},
        ))
        assert right_equation["equivalent"] is True

        nonlinear = _payload(session.call_tool(
            "core.math_check",
            {"text": "Risolvi x*x = 4", "answer": "x=2"},
        ))
        assert nonlinear["recognized"] is False

        safe_hint = _payload(session.call_tool(
            "core.math_hint",
            {"text": "Risolvi 2x + 6 = 20", "attempt": "2x=14, dammi il numero"},
        ))
        assert safe_hint["recognized"] is True
        assert safe_hint["kind"] == "linear_equation"
        assert safe_hint["allow_final_solution"] is False
        assert "x = 7" not in safe_hint["hint"]
        assert "x=7" not in safe_hint["hint"]

        fraction_hint = _payload(session.call_tool(
            "core.math_hint",
            {"text": "Calcola 1/3 + 1/4", "attempt": "sommo sopra e sotto"},
        ))
        assert fraction_hint["recognized"] is True
        assert "denominatore comune" in fraction_hint["hint"]

        turn = _payload(session.call_tool(
            "core.classify_turn",
            {"text": "ma un fazzoletto prende la forma del contenitore ma non è liquido cosa c'entra il ghiaccio"},
        ))
        assert turn["move"] == "counterexample"
        assert turn["confidence"] >= 0.85
        confusion = _payload(session.call_tool(
            "core.classify_turn", {"text": "Non ho capito che cosa significa."}
        ))
        assert confusion["move"] == "confusion"
        example = _payload(session.call_tool(
            "core.classify_turn", {"text": "Fammi un esempio concreto"}
        ))
        assert example["move"] == "request_example"
        explained_example = _payload(session.call_tool(
            "core.classify_turn",
            {"text": "Spiegamelo con un esempio semplice senza formule."},
        ))
        assert explained_example["move"] == "request_example"
        for objection in (
            "Ma a scuola mi dicono sempre passa e cambia segno.",
            "Ma un mazzo di carte ordinato ha meno entropia?",
            "Ma con correlazione così alta una causa ci deve essere per forza.",
            "Ma 5 elementi significa posizioni 1,2,3,4,5.",
            "E la sabbia allora scorre: è un liquido?",
        ):
            classified = _payload(session.call_tool(
                "core.classify_turn", {"text": objection}
            ))
            assert classified["move"] == "counterexample"
        neutral = _payload(session.call_tool(
            "core.classify_turn", {"text": "Oggi ripasso gli stati della materia."}
        ))
        assert neutral["move"] == "neutral"
        concept = _payload(session.call_tool(
            "core.concept_evidence", {"topic": "Gli stati dell'acqua"}
        ))
        assert concept["found"] is True
        assert "flessibile" in concept["evidence"]
        assert "granuli solidi" in concept["evidence"]
        assert "non basta" in concept["evidence"]
        assert "contenitore" in concept["misconceptions"]

        critical_topics = {
            "Forza, accelerazione e velocità": "forza risultante",
            "Equazioni di primo grado": "entrambi i membri",
            "Derivata e monotonia": "punto stazionario",
            "Probabilità condizionata e indipendenza": "P(A∩B)=P(A)P(B)",
            "pH e diluizione": "aumenta il pH",
            "Evoluzione e selezione naturale": "variazioni già presenti",
            "Area del triangolo": "perpendicolare",
            "Luna e luce": "satellite naturale",
            "Entropia e probabilità": "entropia massima",
            "Le stagioni": "inclinazione",
            "Correlazione e causalità": "non dimostra da sola",
            "Indici e array": "0 a n-1",
            "Divisione fra frazioni": "reciproco",
        }
        for topic, expected in critical_topics.items():
            guarded = _payload(session.call_tool(
                "core.concept_evidence", {"topic": topic}
            ))
            assert guarded["found"] is True, topic
            assert expected in guarded["evidence"], topic



def test_core_mcp_idle_sigterm_is_graceful(tmp_path):
    subprocess.run(["make", "-C", str(CORE_DIR)], check=True, capture_output=True)
    socket_path = tmp_path / "core.sock"
    process = subprocess.Popen(
        [str(BINARY), "--socket", str(socket_path)],
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
    )
    try:
        deadline = time.monotonic() + 2.0
        while not socket_path.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert socket_path.exists()
        started = time.monotonic()
        process.send_signal(signal.SIGTERM)
        assert process.wait(timeout=1.5) == 0
        assert time.monotonic() - started < 1.2
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=1)

def test_core_mcp_summary_and_text_profile_are_source_bounded():
    text = (
        "Le piante usano luce, acqua e anidride carbonica. "
        "La fotosintesi produce sostanze organiche. "
        "Il processo sostiene molte catene alimentari. "
        "La clorofilla assorbe parte della luce."
    )
    with _session() as session:
        session.list_tools()
        summary = _payload(session.call_tool(
            "core.extractive_summary", {"text": text, "max_sentences": 2}
        ))
        profile = _payload(session.call_tool("core.text_profile", {"text": text}))
    assert summary["selected_sentences"] <= 2
    for sentence in summary["summary"].split(". "):
        assert sentence.rstrip(".") in text
    assert profile["words"] > 0
    assert 320 <= profile["recommended_chunk_chars"] <= 1200


class NoModel:
    def __init__(self):
        self.calls = 0

    def __call__(self, *_):
        self.calls += 1
        raise AssertionError("LLM should not be called")


class FakeCore:
    def math_check(self, text, answer):
        return {
            "ok": True, "recognized": True, "answer_recognized": True,
            "equivalent": answer.strip() == "4", "expected": 4,
            "writes": 0, "external_side_effects": 0,
        }

    def math_hint(self, text, attempt=""):
        return {
            "ok": True,
            "recognized": True,
            "kind": "arithmetic_expression",
            "hint": "Fai un solo passaggio e fermati prima del risultato finale.",
            "help_level": 1,
            "allow_final_solution": False,
            "writes": 0,
            "external_side_effects": 0,
        }

    def study_plan(self, minutes, mode):
        return {"ok": True, "blocks": [
            {"order": 1, "minutes": minutes, "label": "Studio attivo"}
        ]}

    def extractive_summary(self, text, max_sentences=4):
        return {"ok": True, "summary": text.split(".")[0] + "."}

    def text_profile(self, text):
        return {"ok": True, "recommended_chunk_chars": 400}

    def classify_turn(self, text):
        move = "counterexample" if "c'entra" in text or text.startswith("ma ") else "question"
        return {"ok": True, "move": move, "signal": "test", "confidence": 0.9,
                "writes": 0, "external_side_effects": 0}

    def concept_evidence(self, topic):
        if topic == "Gli stati dell'acqua":
            return {
                "ok": True, "found": True, "topic": topic,
                "evidence": "Un solido può essere flessibile; un liquido fluisce spontaneamente.",
                "misconceptions": "Adattarsi al contenitore non basta a definire un liquido.",
                "writes": 0, "external_side_effects": 0,
            }
        return {"ok": True, "found": False, "topic": topic, "writes": 0, "external_side_effects": 0}


def test_deterministic_math_and_study_plan_bypass_llm(tmp_path):
    model = NoModel()
    teacher = TeacherService(
        TeacherStore(tmp_path / "teacher.sqlite3"),
        model_call=model,
        deterministic_core=FakeCore(),
    )
    student = teacher.login("CORE-CARD", "middle", "2")["student"]
    session = teacher.start_session(
        student["student_id"], "matematica", "aritmetica"
    )["session"]
    checked = teacher.check_answer(
        session["session_id"], "2 + 2", "4"
    )
    hint = teacher.hint(
        session["session_id"], "3/4 + 1/4", "Non so come iniziare"
    )
    plan = teacher.study_plan(
        session["session_id"], "Ripassare le frazioni", 20
    )
    assert checked["correct"] is True
    assert checked["deterministic"] is True
    assert hint["deterministic"] is True
    assert hint["core_evidence"]["allow_final_solution"] is False
    assert "risultato finale" in hint["response"]
    assert plan["deterministic"] is True
    assert model.calls == 0


def test_prepare_reading_uses_core_profile_without_llm(tmp_path):
    model = NoModel()
    teacher = TeacherService(
        TeacherStore(tmp_path / "teacher.sqlite3"),
        model_call=model,
        deterministic_core=FakeCore(),
    )
    student = teacher.login("READ-CARD", "middle", "2")["student"]
    session = teacher.start_session(
        student["student_id"], "italiano", "lettura"
    )["session"]
    out = teacher.prepare_reading(
        session["session_id"], "Paragrafo lungo. " * 80, 1200
    )
    assert out["chunk_chars"] == 400
    assert out["text_profile"]["recommended_chunk_chars"] == 400
    assert model.calls == 0
