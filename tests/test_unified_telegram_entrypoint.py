from __future__ import annotations

from openshell_backend import app as backend
from ralfloop_agent.unified_assistant import runtime


def test_existing_tasks_run_routes_unified_before_generic_gpu_handoff(monkeypatch):
    monkeypatch.setenv("RALFLOOP_UNIFIED_ASSISTANT", "1")
    monkeypatch.setattr(runtime, "is_unified_telegram_request", lambda *_: True)
    monkeypatch.setattr(runtime, "run_unified_telegram", lambda *_: {
        "ok": True, "final_answer": "unified", "interaction_mode": "unified_assistant",
    })

    request = backend.TaskRunRequest(
        user_goal="Quanto fa in soggiorno?",
        extra_context={
            "source": "telegram_natural", "telegram_user_id": 1,
            "telegram_chat_id": 1, "telegram_message_id": 1,
        },
    )
    result = backend.run_task(request)

    assert result["final_answer"] == "unified"
    assert result["interaction_mode"] == "unified_assistant"


def test_route_only_keeps_existing_meowgram_two_step_contract(monkeypatch):
    monkeypatch.setenv("RALFLOOP_UNIFIED_ASSISTANT", "1")
    request = backend.TaskRunRequest(
        user_goal="Accendi luce cucina",
        mode="route_only",
        extra_context={
            "source": "telegram_natural", "telegram_user_id": 1,
            "telegram_chat_id": 1, "telegram_message_id": 1,
        },
    )

    result = backend.run_task(request)

    assert result["stop_reason"] == "route_only"
    assert result["current_role"] == "capability_router"


def test_fastweb_route_only_avoids_meowgram_generic_orchestrate(monkeypatch):
    monkeypatch.setenv("RALFLOOP_UNIFIED_ASSISTANT", "1")
    request = backend.TaskRunRequest(
        user_goal="Controlla se Fastweb ha mai comunicato un aumento",
        mode="route_only",
        extra_context={
            "source": "telegram_natural", "telegram_user_id": 1,
            "telegram_chat_id": 1, "telegram_message_id": 1,
        },
    )

    result = backend.run_task(request)
    route = result["capability_route"]

    assert route["task_mode"] == "tool_backed_read"
    assert route["skills_used"] == ["email.search"]
    assert route["mcp_used"] == ["google_workspace.gmail"]
    assert route["task_mode"] != "check_only"
