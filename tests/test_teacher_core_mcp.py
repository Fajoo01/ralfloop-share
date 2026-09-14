from pathlib import Path
import subprocess

from ralfloop_agent.teacher.service import TeacherService
from ralfloop_agent.teacher.store import TeacherStore
from src.mcp_transport import MCPClientSession, StdioMCPTransport


ROOT = Path(__file__).resolve().parents[1]
CORE_DIR = ROOT / "tools" / "teacher_core_mcp"
BINARY = CORE_DIR / "ralf-teacher-core-mcp"


def _session():
    subprocess.run(["make", "-C", str(CORE_DIR)], check=True, capture_output=True)
    return MCPClientSession(
        StdioMCPTransport([str(BINARY), "--stdio"]),
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
        }
        math = _payload(session.call_tool(
            "core.math_check",
            {"text": "3/4 + 1/4", "answer": "1"},
        ))
        assert math["recognized"] is True
        assert math["answer_recognized"] is True
        assert math["equivalent"] is True
        assert math["expected"] == 1


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

    def study_plan(self, minutes, mode):
        return {"ok": True, "blocks": [
            {"order": 1, "minutes": minutes, "label": "Studio attivo"}
        ]}

    def extractive_summary(self, text, max_sentences=4):
        return {"ok": True, "summary": text.split(".")[0] + "."}

    def text_profile(self, text):
        return {"ok": True, "recommended_chunk_chars": 400}


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
    plan = teacher.study_plan(
        session["session_id"], "Ripassare le frazioni", 20
    )
    assert checked["correct"] is True
    assert checked["deterministic"] is True
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
