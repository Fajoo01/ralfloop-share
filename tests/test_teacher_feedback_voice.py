from pathlib import Path

import pytest

from ralfloop_agent.teacher.web.feedback_voice import FeedbackVoiceRegistry


class FakeFish:
    def __init__(self, status="pending"):
        self.status = status
        self.prepared = []

    def prepare(self, text):
        self.prepared.append(text)
        return {"status": self.status}

    def ready_path(self, text):
        return Path("/tmp/teacher-peppone.wav") if self.status == "ready" else None


def test_disabled_fish_preserves_existing_teacher_payload():
    fish = FakeFish("browser_fallback")
    registry = FeedbackVoiceRegistry(fish)
    result = {"feedback": "Spiegazione breve.", "source": "teacher"}

    assert registry.attach("student-a", result) == result
    assert fish.prepared == ["Spiegazione breve."]


def test_feedback_wrapper_is_removed_before_ui_and_tts():
    fish = FakeFish("browser_fallback")
    registry = FeedbackVoiceRegistry(fish)
    wrapped = '{\n  "response": "Prima riga.\n\nSeconda riga."\n}'

    result = registry.attach("student-a", {"feedback": wrapped, "source": "teacher"})

    assert result["feedback"] == "Prima riga.\n\nSeconda riga."
    assert fish.prepared == ["Prima riga.\n\nSeconda riga."]


def test_malformed_multiline_feedback_wrapper_is_salvaged():
    fish = FakeFish("browser_fallback")
    registry = FeedbackVoiceRegistry(fish)
    wrapped = '{\n  "response": "Prima riga.\n\nSeconda riga con accento: perché."\n}'
    # Make it invalid JSON exactly like a model response containing literal
    # newlines inside the quoted response value.
    wrapped = wrapped.replace("\\n", "\n")

    result = registry.attach("student-a", {"feedback": wrapped})

    assert result["feedback"] == "Prima riga.\n\nSeconda riga con accento: perché."


def test_pending_voice_uses_opaque_student_bound_waitable_handle():
    fish = FakeFish("pending")
    registry = FeedbackVoiceRegistry(fish, ready_wait_seconds=0)

    result = registry.attach("student-a", {"feedback": "Spiegazione breve."})
    voice = result["voice"]

    assert voice["status"] == "ready"
    assert len(voice["id"]) == 32
    assert "Spiegazione breve." not in repr(voice)
    assert voice["url"] == f"/api/feedback-audio/{voice['id']}/file"
    assert registry.prepare("student-a", voice["id"])["status"] == "pending"
    assert registry.ready_path("student-a", voice["id"]) is None
    with pytest.raises(LookupError):
        registry.prepare("student-b", voice["id"])


def test_ready_voice_returns_only_same_origin_file_url():
    fish = FakeFish("ready")
    registry = FeedbackVoiceRegistry(fish)

    result = registry.attach("student-a", {"feedback": "Ben fatto."})
    voice = result["voice"]

    assert voice["status"] == "ready"
    assert voice["url"] == f"/api/feedback-audio/{voice['id']}/file"
    assert registry.prepare("student-a", voice["id"])["url"] == voice["url"]
    assert registry.ready_path("student-a", voice["id"]) == Path("/tmp/teacher-peppone.wav")


def test_malformed_voice_handle_is_rejected():
    registry = FeedbackVoiceRegistry(FakeFish("ready"))

    with pytest.raises(LookupError):
        registry.prepare("student-a", "not-a-handle")


def test_default_wait_window_covers_real_cold_start():
    registry = FeedbackVoiceRegistry(FakeFish("pending"))
    assert registry.ready_wait_seconds >= 60.0
