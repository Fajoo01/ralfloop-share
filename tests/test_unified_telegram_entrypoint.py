from __future__ import annotations

from openshell_backend import app as backend
from ralfloop_agent.cli.session_store import SessionStore
from ralfloop_agent.unified_assistant import runtime
from ralfloop_agent.unified_assistant.contracts import PolicyClass
from ralfloop_agent.unified_assistant.conversation import SessionConversationAdapter


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


def test_tasks_run_routes_bare_approval_for_exact_pending_session(monkeypatch, tmp_path):
    monkeypatch.setenv("RALFLOOP_UNIFIED_ASSISTANT", "1")
    monkeypatch.setenv("RALFLOOP_UNIFIED_SESSION_DIR", str(tmp_path / "sessions"))
    store = SessionStore(tmp_path / "sessions")
    runtime._ensure_session(store, "telegram-22-11")
    adapter = SessionConversationAdapter(store)
    conversation = adapter.load("telegram-22-11")
    pending = conversation.stage(
        domain="mailchimp", action="mailchimp_campaign_create",
        policy=PolicyClass.CONFIRM_WRITE,
        payload={"list_id": "audience_1", "subject": "Approved"},
        displayed_text="Approved",
    )
    conversation.attach_approval_request(
        domain="mailchimp", pending_id=pending.pending_id,
        payload_digest=pending.payload_digest, approval_ref="apr_ABCDEFGH",
        created_at=pending.created_at, expires_at=pending.expires_at,
    )
    adapter.save("telegram-22-11", conversation)
    monkeypatch.setattr(runtime, "run_unified_telegram", lambda *_: {
        "ok": True, "final_answer": "unified-confirmation",
        "interaction_mode": "unified_assistant",
    })

    result = backend.run_task(backend.TaskRunRequest(
        user_goal="approvo",
        extra_context={
            "source": "telegram_natural", "telegram_user_id": 11,
            "telegram_chat_id": 22, "telegram_message_id": 2,
        },
    ))

    assert result["final_answer"] == "unified-confirmation"
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


def test_route_only_uses_capability_rag_for_atm_sonia(monkeypatch):
    monkeypatch.setenv("RALFLOOP_UNIFIED_ASSISTANT", "1")
    result = backend.run_task(backend.TaskRunRequest(
        user_goal="Portami da Sonia", mode="route_only",
        extra_context={"source": "telegram_natural", "telegram_user_id": 1,
                       "telegram_chat_id": 1, "telegram_message_id": 1},
    ))
    route = result["capability_route"]
    assert route["task_mode"] == "tool_backed_read"
    assert route["intent"] == "atm.route"
    assert route["skills_used"] == ["atm.route"]
    assert route["mcp_connectors"] == ["atm.route.mcp"]


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



def test_generic_pec_read_enters_unified_runtime_not_generic_failure_loop(monkeypatch):
    monkeypatch.setenv("RALFLOOP_UNIFIED_ASSISTANT", "1")

    seen = {}

    def fake_run_unified(text, context):
        seen["text"] = text
        seen["source"] = context.get("source")
        return {
            "ok": True,
            "final_answer": "PEC_UNIFIED_READ_OK",
            "response": "PEC_UNIFIED_READ_OK",
            "interaction_mode": "unified_assistant",
            "capability": "pec_discover_messages",
            "approval_required": False,
            "metadata": {
                "mcp_invoked": True,
                "selected_capability": "pec_discover_messages",
                "writes": 0,
            },
        }

    monkeypatch.setattr(runtime, "run_unified_telegram", fake_run_unified)

    request = backend.TaskRunRequest(
        user_goal="Cosa dice la PEC che è appena arrivata?",
        extra_context={
            "source": "telegram_natural",
            "telegram_user_id": 1,
            "telegram_chat_id": 1,
            "telegram_message_id": 99,
        },
    )

    result = backend.run_task(request)

    assert seen == {
        "text": "Cosa dice la PEC che è appena arrivata?",
        "source": "telegram_natural",
    }
    assert result["ok"] is True
    assert result["final_answer"] == "PEC_UNIFIED_READ_OK"
    assert result["interaction_mode"] == "unified_assistant"
    assert result["capability"] == "pec_discover_messages"
    assert result["metadata"]["writes"] == 0
    assert result.get("stop_reason") != "repeated_failure"


def test_generic_pec_route_only_exposes_inbox_read_metadata(monkeypatch):
    monkeypatch.setenv("RALFLOOP_UNIFIED_ASSISTANT", "1")

    result = backend.run_task(
        backend.TaskRunRequest(
            user_goal="Cosa dice la PEC che è appena arrivata?",
            mode="route_only",
            extra_context={
                "source": "telegram_natural",
                "telegram_user_id": 1,
                "telegram_chat_id": 1,
                "telegram_message_id": 100,
            },
        )
    )

    route = result["capability_route"]
    assert result["stop_reason"] == "route_only"
    assert route["task_mode"] == "tool_backed_read"
    assert route["intent"] == "pec.read"
    assert route["domains"] == ["pec"]
    assert route["skills_used"] == ["pec.read"]
    assert route["mcp_used"] == ["pec.read.mcp"]
    assert route["write_policy"] == "no_write"
    assert route["requires_confirmation"] is False
