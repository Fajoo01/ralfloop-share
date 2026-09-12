from fastapi.testclient import TestClient

from ralfloop_agent.teacher.web.api import create_app
from ralfloop_agent.teacher.web.application import LearningApplication
from ralfloop_agent.teacher.web.client import DemoTeacher
from ralfloop_agent.teacher.web.state import State


class FakeFish:
    def __init__(self, wav):
        self.wav = wav
        self.texts = []

    def prepare(self, text):
        self.texts.append(text)
        return {"status": "ready"}

    def ready_path(self, text):
        self.texts.append(text)
        return self.wav


def login(client, card):
    response = client.post(
        "/api/login",
        json={"membership_card_id": card, "credential": "Credential!123"},
    )
    assert response.status_code == 200


def test_audio_prepare_uses_only_owned_track_and_fixed_server_voice(tmp_path):
    state = State(tmp_path / "student.sqlite3")
    for card in ("A", "B"):
        state.register(card, "Credential!123", "Demo " + card, "middle", 2, demo=True)
    student = state.authenticate(state.login("A", "Credential!123"))
    learning = LearningApplication(state, DemoTeacher())
    material = learning.add_material(
        student,
        "Appunti",
        "Le frazioni hanno numeratore e denominatore.",
        "own",
    )
    audio = learning.material_action(student, material["material_id"], "audio")

    wav = tmp_path / "ready.wav"
    wav.write_bytes(b"RIFF" + b"0" * 4 + b"WAVE" + b"0" * 40)
    fish = FakeFish(wav)

    with TestClient(
        create_app(state, DemoTeacher(), origin="http://testserver", fish_tts=fish),
        raise_server_exceptions=False,
    ) as client:
        client.headers.update({"origin": "http://testserver", "x-teacher-request": "1"})
        login(client, "A")
        endpoint = f"/api/audio/{audio['audio_id']}/prepare"
        prepared = client.post(endpoint, json={"chapter": 0})
        assert prepared.status_code == 200
        body = prepared.json()
        assert body["status"] == "ready"
        assert body["url"] == f"/api/audio/{audio['audio_id']}/file/0"
        assert "peppone" not in prepared.text.lower()
        assert "TEACHER_FISH" not in prepared.text
        assert fish.texts[0] == audio["tracks"][0]["text"]

        served = client.get(body["url"])
        assert served.status_code == 200
        assert served.headers["content-type"].startswith("audio/wav")
        assert served.content[:4] == b"RIFF"

        assert client.post(endpoint, json={"chapter": 0, "text": "testo scelto dal browser"}).status_code == 422
        assert client.post(endpoint, json={"chapter": 0, "voice": "other"}).status_code == 422

        login(client, "B")
        assert client.post(endpoint, json={"chapter": 0}).status_code == 404
        assert client.get(body["url"]).status_code == 404
