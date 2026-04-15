from __future__ import annotations

import requests

from ralfloop_agent.providers.ollama import OllamaPlanner


class _DummyResponse:
    def raise_for_status(self):
        return None

    def json(self):
        return {"response": '{"tool_name":"sandbox_list_dir","tool_input":{"path":"."},"why":"ok"}'}


def test_runtime_prompt_styles_change_shape(monkeypatch) -> None:
    captured: list[str] = []

    def fake_post(url, json=None, timeout=None):
        captured.append(json["prompt"])
        return _DummyResponse()

    monkeypatch.setattr(requests, "post", fake_post)

    planner = OllamaPlanner(model="stub")
    planner.role_name = "planner"
    planner.internal_prompt_style = "standard"
    planner.choose_next_action("Organizza il lavoro in modo prudente", 3)

    coder = OllamaPlanner(model="stub")
    coder.role_name = "coder"
    coder.internal_prompt_style = "caveman"
    coder.choose_next_action("Organizza il lavoro in modo prudente", 3)

    judge = OllamaPlanner(model="stub")
    judge.role_name = "judge"
    judge.internal_prompt_style = "cinese"
    judge.choose_next_action("Organizza il lavoro in modo prudente", 3)

    assert len(captured) == 3
    assert "Sei un planner" in captured[0]
    assert "Tu coder sandbox" in captured[1]
    assert "Ruolo=judge" in captured[2]
    assert captured[0] != captured[1] != captured[2]
