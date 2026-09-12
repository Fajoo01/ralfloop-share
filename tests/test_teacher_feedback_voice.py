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


def test_pending_voice_uses_opaque_student_bound_handle():
    fish = FakeFish("pending")
    registry = FeedbackVoiceRegistry(fish)

    result = registry.attach("student-a", {"feedback": "Spiegazione breve."})
    voice = result["voice"]

    assert voice["status"] == "pending"
    assert len(voice["id"]) == 32
    assert "Spiegazione breve." not in repr(voice)
    assert registry.prepare("student-a", voice["id"])["status"] == "pending"
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
