from ralfloop_agent.unified_assistant.core import _recipient_label
from ralfloop_agent.unified_assistant.runtime import (
    is_unified_telegram_request,
    unified_route_probe,
)


GOAL = """Rispondi alla mail di Caterina Ghirelli del 5 agosto 2026 relativa
all'invito a Passi da Gigante del 13 settembre 2026 al Circolo Magnolia.
Destinataria verificata: caterina@circolomagnolia.it
Messaggio/thread Gmail sorgente: 19fd1fbc9ff936d0"""


def test_magnolia_reply_enters_unified_and_routes_as_reply(monkeypatch):
    monkeypatch.setenv("RALFLOOP_UNIFIED_ASSISTANT", "1")
    monkeypatch.setenv("RALFLOOP_EMAIL_ASSISTANT_LIVE", "1")

    context = {
        "source": "telegram_natural",
        "telegram_user_id": 11,
        "telegram_chat_id": 22,
        "telegram_message_id": 33,
    }

    assert is_unified_telegram_request(GOAL, context) is True

    route = unified_route_probe(GOAL, context)

    assert route is not None
    assert route["task_mode"] == "external_action"
    assert route["interaction_class"] == "EXTERNAL_ACTION"
    assert route["intent"] == "email.reply"
    assert route["skills_used"] == ["email.reply"]
    assert route["mcp_connectors"] == ["google_workspace.gmail"]
    assert route["write_policy"] == "policy_gated"
    assert route["requires_confirmation"] is True


def test_magnolia_reply_recipient_label_is_bounded():
    assert _recipient_label(GOAL) == "Caterina Ghirelli"
