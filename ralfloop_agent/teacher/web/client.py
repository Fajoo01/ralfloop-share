"""Trusted server-side Teacher bridge; never accepts browser tool names or IDs."""
from contextlib import contextmanager
import json

from src.mcp_transport import MCPClientSession, UnixMCPTransport
from src.teacher import ALL_TOOLS


class TeacherClient:
    def __init__(self, socket_path="/run/ralf-teacher-mcp/mcp.sock", timeout=45, transport_factory=None):
        self.factory = transport_factory or (lambda: UnixMCPTransport(socket_path))
        self.timeout = timeout

    @contextmanager
    def connection(self):
        session = MCPClientSession(self.factory(), timeout=self.timeout, client_name="teacher-student-web")
        try:
            session.initialize()
            surface = {tool.name for tool in session.list_tools()}
            if surface != set(ALL_TOOLS):
                raise ValueError("teacher_surface_mismatch")
            yield session
        finally:
            session.close()

    @staticmethod
    def call(session, name, arguments):
        if name not in ALL_TOOLS:
            raise ValueError("tool_not_allowed")
        raw = session.call_tool(name, arguments)
        out = raw.get("structuredContent")
        if out is None:
            out = json.loads(raw["content"][0]["text"])
        if not isinstance(out, dict) or out.get("ok") is not True:
            raise ValueError("invalid_teacher_result")
        return out

    def health(self):
        with self.connection():
            return True

    def perform(self, student, topic, name, arguments):
        if name not in ALL_TOOLS or name in ("teacher.login", "teacher.start_session", "teacher.end_session"):
            raise ValueError("operation_not_allowed")
        with self.connection() as session:
            login = self.call(session, "teacher.login", {
                # Stable pseudonymous identity; raw membership card never reaches prompts.
                "card_id": "web:" + student["id"], "school_level": {"primary":"primaria", "middle":"secondaria I grado", "upper":"secondaria II grado"}[student["school_level"]], "class_year": str(student["grade"])})
            teacher_id = login["student"]["student_id"]
            if name == "teacher.student_progress":
                return self.call(session, name, {"student_id": teacher_id})
            started = self.call(session, "teacher.start_session", {"student_id": teacher_id, "subject": topic["subject"], "topic": topic["title"]})
            sid = started["session"]["session_id"]
            try:
                return self.call(session, name, {**arguments, "session_id": sid})
            finally:
                try:
                    self.call(session, "teacher.end_session", {"session_id": sid})
                except Exception:
                    # Do not overwrite a valid teaching response with cleanup failure.
                    pass


class DemoTeacher:
    """Explicit offline demonstration; never selected as production default."""
    def health(self):
        return False

    def perform(self, student, topic, name, arguments):
        if name not in ALL_TOOLS:
            raise ValueError("tool_not_allowed")
        if name == "teacher.prepare_reading":
            text = arguments["material"]
            return {"ok": True, "chunks": [text[i:i+600] for i in range(0, len(text), 600)], "tts_status": "stub"}
        if name == "teacher.check_answer":
            # No fake semantic grading: the UI offers retry when offline.
            raise ConnectionError("semantic_teacher_unavailable")
        if name in ("teacher.generate_exercise", "teacher.quiz"):
            return {"ok": True, "response": "{invalid_offline_output}"}
        return {"ok": True, "response": "Modalità demo: " + ("moltiplica numeratore e denominatore per lo stesso numero." if topic["id"] == "fractions" else "collega il risultato alla grandezza che hai modificato.")}
