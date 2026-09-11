"""Opt-in, low-cost production MCP test. Only anonymous demo learning records are created."""
import os

import pytest
from fastapi.testclient import TestClient

from ralfloop_agent.teacher.web.api import create_app
from ralfloop_agent.teacher.web.client import TeacherClient
from ralfloop_agent.teacher.web.state import State


@pytest.mark.integration
@pytest.mark.skipif(os.environ.get("TEACHER_WEB_LIVE") != "1", reason="Explicit live Teacher opt-in required")
def test_web_api_real_mcp_exercise_check_and_update(tmp_path):
    state=State(tmp_path / "web.sqlite3")
    state.register("E2E-DEMO", "E2EDemo!Only", "Teacher E2E Demo", "middle", 2, demo=True)
    class ControlledExerciseClient(TeacherClient):
        def perform(self, student, topic, name, arguments):
            if name == "teacher.generate_exercise":
                arguments = {**arguments, "topic": ("Esercizio obbligatorio: Quanto fa 1/2 + 1/2? Spiega il risultato. " + arguments["topic"])[:1000]}
            result = super().perform(student, topic, name, arguments)
            if name == "teacher.generate_exercise": self.generated_response = result.get("response")
            return result
    teacher=ControlledExerciseClient(timeout=90)
    with TestClient(create_app(state,teacher,origin="http://testserver")) as client:
        client.headers.update({"origin":"http://testserver","x-teacher-request":"1"})
        assert client.post("/api/login",json={"membership_card_id":"E2E-DEMO","credential":"E2EDemo!Only"}).status_code == 200
        assert client.get("/health").json() == {"status":"ok"}
        generated=client.post("/api/activities",json={"topic":"fractions","activity_type":"free_answer"})
        assert generated.status_code == 200, generated.text
        activity=generated.json()
        assert activity["source"] == "teacher_generated", getattr(teacher,"generated_response",activity)
        assert "1/2" in activity["instructions"]
        result=client.post(f"/api/activities/{activity['activity_id']}/answer",json={"answer":"1, perché due metà formano un intero.","request_key":"real-mcp-answer"})
        assert result.status_code == 200, result.text
        assert result.json()["correct"] is True, result.text
        assert result.json()["xp_awarded"] > 0
        assert client.get("/api/progress").json()["topics"][0]["success_count"] == 1
