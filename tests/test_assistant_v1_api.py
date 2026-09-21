from __future__ import annotations

from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from openshell_backend import assistant_v1_api
from ralfloop_agent.providers.chat import ChatResult
from ralfloop_agent.unified_assistant.contracts import AssistantFeatureFlags


class FakeProvider:
    name = "fake_local"
    default_model = "fast-local"

    def __init__(self, text: str = "risposta locale") -> None:
        self.text = text
        self.calls: list[tuple[list[dict[str, str]], str | None]] = []

    def chat(self, messages, *, model=None):
        self.calls.append((list(messages), model))
        return ChatResult(
            text=self.text,
            model=model or self.default_model,
            provider=self.name,
            metadata={"endpoint": "http://127.0.0.1:9999"},
        )
def _client(
    provider: FakeProvider,
    *,
    route_probe,
    unified_runner,
) -> TestClient:
    app = FastAPI()
    app.include_router(assistant_v1_api.router)
    app.dependency_overrides[assistant_v1_api.get_chat_provider] = lambda: provider
    app.dependency_overrides[assistant_v1_api.get_assistant_flags] = lambda: AssistantFeatureFlags(
        unified_assistant=True
    )
    app.dependency_overrides[assistant_v1_api.get_unified_route_probe] = lambda: route_probe
    app.dependency_overrides[assistant_v1_api.get_unified_runner] = lambda: unified_runner
    return TestClient(app)


def _no_route(*_args, **_kwargs):
    return None


def _unexpected_unified(*_args, **_kwargs):
    raise AssertionError("unified runner must not be called")


def test_general_chat_uses_local_provider() -> None:
    provider = FakeProvider("Ciao dal locale")
    response = _client(
        provider,
        route_probe=_no_route,
        unified_runner=_unexpected_unified,
    ).post("/assistant/v1/chat", json={"message": "Spiegami la fotosintesi"})

    assert response.status_code == 200
    payload = response.json()
    assert payload["response"] == "Ciao dal locale"
    assert payload["route"] == "local_chat"
    assert payload["metadata"]["local_only"] is True
    assert payload["metadata"]["model_lane"] == "general"
    assert len(provider.calls) == 1


def test_general_knowledge_selects_general_model(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BOTTAZZI_ASSISTANT_GENERAL_MODEL", "qwen3.5:9b")
    provider = FakeProvider("APS = Associazione di Promozione Sociale")
    payload = _client(
        provider,
        route_probe=_no_route,
        unified_runner=_unexpected_unified,
    ).post(
        "/assistant/v1/chat",
        json={"message": "Cos'è una APS in Italia?"},
    ).json()

    assert payload["route"] == "local_chat"
    assert payload["model"] == "qwen3.5:9b"
    assert provider.calls[0][1] == "qwen3.5:9b"
    assert payload["metadata"]["model_lane"] == "general"
    assert payload["metadata"]["routing_reason"] == "general_knowledge_or_ambiguity"


def test_acronym_alone_uses_general_lane(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BOTTAZZI_ASSISTANT_GENERAL_MODEL", "qwen3.5:9b")
    provider = FakeProvider()
    payload = _client(
        provider,
        route_probe=_no_route,
        unified_runner=_unexpected_unified,
    ).post("/assistant/v1/chat", json={"message": "APS"}).json()

    assert payload["metadata"]["model_lane"] == "general"
    assert provider.calls[0][1] == "qwen3.5:9b"


def test_fast_mode_overrides_general_heuristic(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BOTTAZZI_ASSISTANT_FAST_MODEL", "qwen2.5:3b")
    monkeypatch.setenv("BOTTAZZI_ASSISTANT_GENERAL_MODEL", "qwen3.5:9b")
    provider = FakeProvider()
    payload = _client(
        provider,
        route_probe=_no_route,
        unified_runner=_unexpected_unified,
    ).post(
        "/assistant/v1/chat",
        json={"message": "Cos'è una APS in Italia?", "mode": "fast"},
    ).json()

    assert payload["metadata"]["model_lane"] == "fast"
    assert provider.calls[0][1] == "qwen2.5:3b"


def test_unified_route_bypasses_chat_provider() -> None:
    provider = FakeProvider()

    def route_probe(*_args, **_kwargs):
        return {"intent": "bandi.eligibility", "task_mode": "tool_backed_read"}

    def unified_runner(_message, _context, **_kwargs):
        return {
            "ok": True,
            "final_answer": "Bando verificato.",
            "approval_required": False,
            "metadata": {"selected_skill": "bandi.eligibility", "tools_executed": True},
        }

    response = _client(provider, route_probe=route_probe, unified_runner=unified_runner).post(
        "/assistant/v1/chat",
        json={"message": "Controlla se Tiremm può partecipare al bando", "session_id": "s1"},
    )

    payload = response.json()
    assert payload["route"] == "unified"
    assert payload["response"] == "Bando verificato."
    assert payload["provider"] == "unified_assistant"
    assert provider.calls == []


def test_protected_unified_action_remains_approval_bound() -> None:
    provider = FakeProvider()

    def route_probe(*_args, **_kwargs):
        return {"intent": "email.compose", "task_mode": "external_action"}

    def unified_runner(_message, _context, **_kwargs):
        return {
            "ok": True,
            "response": "Bozza pronta. Serve approvazione.",
            "approval_required": True,
            "metadata": {"status": "draft_pending_approval", "send_calls": 0},
        }

    payload = _client(provider, route_probe=route_probe, unified_runner=unified_runner).post(
        "/assistant/v1/chat", json={"message": "Scrivi una mail a Sonia"}
    ).json()

    assert payload["route"] == "unified"
    assert payload["approval_required"] is True
    assert payload["metadata"]["send_calls"] == 0
    assert provider.calls == []
def test_deep_mode_selects_configured_local_model(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BOTTAZZI_ASSISTANT_DEEP_MODEL", "qwen35-local")
    provider = FakeProvider("analisi")
    payload = _client(
        provider,
        route_probe=_no_route,
        unified_runner=_unexpected_unified,
    ).post(
        "/assistant/v1/chat",
        json={"message": "Analizza questa architettura", "mode": "deep"},
    ).json()

    assert payload["route"] == "deep_chat"
    assert payload["model"] == "qwen35-local"
    assert provider.calls[0][1] == "qwen35-local"
    assert payload["metadata"]["reasoning_mode"] == "deep"


def test_allow_tools_false_forces_read_only_chat() -> None:
    provider = FakeProvider()
    calls = []

    def route_probe(*args, **kwargs):
        calls.append((args, kwargs))
        return {"intent": "email.compose"}

    payload = _client(
        provider,
        route_probe=route_probe,
        unified_runner=_unexpected_unified,
    ).post(
        "/assistant/v1/chat",
        json={"message": "Scrivi una mail a Sonia", "allow_tools": False},
    ).json()

    assert payload["route"] == "local_chat"
    assert calls == []
    assert payload["metadata"]["tools_allowed"] is False


@pytest.mark.parametrize(
    ("message", "expected_domain"),
    [
        ("Analizza il repository e fai debug del codice", "code"),
        ("Riassumi il documento PDF allegato", "documents"),
        ("Fai una ricerca approfondita sul nuovo standard", "research"),
        ("Controlla i servizi del server e lo spazio disco", "infrastructure"),
    ],
)
def test_assistant_v1_extended_gate_reuses_unified_domains(
    message: str,
    expected_domain: str,
) -> None:
    route = assistant_v1_api.unified_route_probe(
        message,
        {
            "source": "ralf_terminal",
            "assistant_surface": "assistant_v1",
            "session_id": "route-bench",
        },
        flags_override=AssistantFeatureFlags(unified_assistant=True),
    )

    assert route is not None
    assert expected_domain in route["domains"]


def test_assistant_v1_extended_gate_does_not_expand_other_surfaces() -> None:
    route = assistant_v1_api.unified_route_probe(
        "Riassumi il documento PDF allegato",
        {"source": "ralf_terminal", "assistant_surface": "legacy"},
        flags_override=AssistantFeatureFlags(unified_assistant=True),
    )

    assert route is None


def test_local_chat_cannot_claim_external_execution() -> None:
    provider = FakeProvider("Ho inviato la mail.")
    payload = _client(
        provider,
        route_probe=_no_route,
        unified_runner=_unexpected_unified,
    ).post("/assistant/v1/chat", json={"message": "Cosa hai fatto?"}).json()

    assert payload["metadata"]["execution_claim_blocked"] is True
    assert "inviato" not in payload["response"].casefold()


def test_fast_lane_can_use_dedicated_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BOTTAZZI_ASSISTANT_FAST_MODEL", "qwen2.5-3b")
    default = FakeProvider("default")
    fast = FakeProvider("fast")
    client = _client(default, route_probe=_no_route, unified_runner=_unexpected_unified)
    client.app.dependency_overrides[assistant_v1_api.get_fast_lane_provider] = lambda: fast

    payload = client.post("/assistant/v1/chat", json={"message": "ciao"}).json()

    assert payload["response"] == "fast"
    assert payload["metadata"]["model_lane"] == "fast"
    assert payload["metadata"]["lane_provider"] == "fake_local"
    assert fast.calls[0][1] == "qwen2.5-3b"
    assert default.calls == []


def test_explicit_model_bypasses_dedicated_fast_provider() -> None:
    default = FakeProvider("explicit")
    fast = FakeProvider("fast")
    client = _client(default, route_probe=_no_route, unified_runner=_unexpected_unified)
    client.app.dependency_overrides[assistant_v1_api.get_fast_lane_provider] = lambda: fast

    payload = client.post(
        "/assistant/v1/chat",
        json={"message": "ciao", "model": "manual-model", "mode": "fast"},
    ).json()

    assert payload["response"] == "explicit"
    assert default.calls[0][1] == "manual-model"
    assert fast.calls == []


def test_normative_admin_question_enters_grounded_unified_route() -> None:
    route = assistant_v1_api.unified_route_probe(
        "Cos'è una APS in Italia?",
        {
            "source": "ralf_terminal",
            "assistant_surface": "assistant_v1",
            "session_id": "grounded-aps",
        },
        flags_override=AssistantFeatureFlags(unified_assistant=True),
    )

    assert route is not None
    assert route["intent"] == "research.deep"
    assert route["skills_used"] == ["research.deep"]
    assert route["task_mode"] == "tool_backed_read"
    assert route["mcp_connectors"] == ["model_tool.deep_web_research"]
    assert route["requires_confirmation"] is False


def test_bottazzi_ui_is_served_by_assistant_v1_router() -> None:
    client = _client(
        FakeProvider(),
        route_probe=_no_route,
        unified_runner=_unexpected_unified,
    )
    response = client.get("/assistant/v1")

    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "Bot-tazzi" in response.text
    assert "Assistente autonomo" in response.text
    assert "/assistant/v1/chat" in response.text
    assert "localStorage" in response.text
    assert "openai.com" not in response.text.casefold()


def test_assistant_status_declares_local_branded_surface() -> None:
    payload = assistant_v1_api.assistant_v1_status()

    assert payload["assistant_name"] == "Bot-tazzi"
    assert payload["local_only"] is True
    assert payload["cloud_llm_required"] is False
    assert payload["ui_path"] == "/assistant/v1"
