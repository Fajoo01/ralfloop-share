from __future__ import annotations

import json

import pytest

from ralfloop_agent.providers.inference_runtime import (
    GenerateRequest,
    OpenAICompatRuntime,
)
from ralfloop_agent.providers.ollama import OllamaPlanner


def test_agent_uses_live_llama_cpp_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RALF_CHAT_PROVIDER", "llama_cpp")
    monkeypatch.setenv("RALF_LLAMA_CPP_BASE_URL", "http://127.0.0.1:19091")
    monkeypatch.setenv("RALF_LLAMA_CPP_MODEL", "qwen3:8b")

    planner = OllamaPlanner(model="qwen2.5:7b")

    assert isinstance(planner.runtime, OpenAICompatRuntime)
    assert planner.runtime.base_url == "http://127.0.0.1:19091"
    assert planner.model == "qwen3:8b"
    assert planner.runtime.model == "qwen3:8b"


def test_llama_cpp_selection_does_not_fallback_to_ollama(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RALF_CHAT_PROVIDER", "llama_cpp")
    monkeypatch.setenv("RALF_LLAMA_CPP_BASE_URL", "http://127.0.0.1:19091")
    planner = OllamaPlanner()

    assert planner.runtime.name == "openai_compat"
    assert planner.runtime.base_url != "http://127.0.0.1:11434"


def test_openai_compat_transports_json_schema(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def read(self) -> bytes:
            return b'{"choices":[{"message":{"content":"{}"}}]}'

    def fake_urlopen(req, timeout):
        captured["payload"] = json.loads(req.data.decode("utf-8"))
        captured["timeout"] = timeout
        return Response()

    monkeypatch.setattr(
        "ralfloop_agent.providers.inference_runtime.request.urlopen",
        fake_urlopen,
    )
    schema = {
        "type": "object",
        "properties": {"tool_name": {"type": "string"}},
        "required": ["tool_name"],
        "additionalProperties": False,
    }
    runtime = OpenAICompatRuntime(
        base_url="http://127.0.0.1:19091",
        model="qwen3:8b",
        timeout_sec=17,
    )

    result = runtime.generate(
        GenerateRequest(model="qwen3:8b", prompt="plan", format=schema)
    )

    assert result.runtime == "openai_compat"
    assert captured["timeout"] == 17
    payload = captured["payload"]
    assert isinstance(payload, dict)
    assert payload["model"] == "qwen3:8b"
    assert payload["response_format"]["type"] == "json_schema"
    assert payload["response_format"]["json_schema"]["schema"] == schema
