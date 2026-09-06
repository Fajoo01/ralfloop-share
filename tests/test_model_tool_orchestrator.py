from __future__ import annotations

from contextlib import nullcontext
from pathlib import Path
import sys

from ralfloop_agent.model_tools import (
    ModelToolManager,
    ModelToolOrchestrator,
    ModelToolRegistry,
    ModelToolSpec,
    ResourceDecision,
)
from ralfloop_agent.providers.chat import ChatResult
from ralfloop_agent.model_tools.orchestrator import _strict_decision


RUNTIME_TRUNCATED_DECISION = (
    '{"action":"tool","response":null,"tool_id":"deep_web_research_agentcpm_v1",'
    '"arguments":{"query":"quantizzazione quantistica o quantizzazione numerica in '
    'informatica e fisica?","domains":[],"max_sources":5,"max_steps":3,'
    '"seed_urls":[],"query":"quantizzazione"}'
)


class SequenceProvider:
    name = "llama_cpp"
    default_model = "qwen-test"

    def __init__(self, responses: list[str]) -> None:
        self.responses = list(responses)
        self.calls = []

    def chat(self, messages, *, model=None):
        self.calls.append(list(messages))
        return ChatResult(
            self.responses.pop(0),
            "qwen-test",
            self.name,
            {"endpoint": "http://127.0.0.1:19091"},
        )

    def stream_chat(self, messages, *, model=None):
        raise AssertionError("stream_not_expected")


def _registry(tmp_path: Path) -> tuple[ModelToolRegistry, ModelToolSpec]:
    spec = ModelToolSpec.model_validate(
        {
            "tool_id": "semantic_v1",
            "capability": "semantic_search",
            "model_id": "fixture/semantic",
            "revision": "rev1",
            "backend": "fixture",
            "python_executable": sys.executable,
            "device": "cpu",
            "timeout_sec": 5,
            "max_ram_mb": 1,
            "max_vram_mb": 0,
            "local_files_only": True,
            "trust_remote_code": False,
            "enabled": True,
            "status": "ready",
            "model_card_status": "fixture",
            "license": "fixture",
            "input_schema": {
                "type": "object",
                "required": ["query", "documents"],
                "additionalProperties": False,
                "properties": {"query": {"type": "string"}, "documents": {"type": "array"}},
            },
            "output_schema": {
                "type": "object",
                "required": ["ranked"],
                "additionalProperties": False,
                "properties": {"ranked": {"type": "array"}},
            },
        }
    )
    snapshot = tmp_path / "models--fixture--semantic" / "snapshots" / "rev1"
    snapshot.mkdir(parents=True)
    (snapshot / "model.safetensors").write_bytes(b"fixture")
    return ModelToolRegistry([spec], cache_root=tmp_path), spec


def _deep_registry(tmp_path: Path) -> ModelToolRegistry:
    registry = ModelToolRegistry.load(Path("config/model_tools.json"), cache_root=tmp_path)
    spec = registry.get("deep_web_research_agentcpm_v1")
    snapshot = tmp_path / "models--openbmb--AgentCPM-Explore-GGUF" / "snapshots" / spec.revision
    snapshot.mkdir(parents=True)
    (snapshot / "AgentCPM-Explore.Q4_K_M.gguf").write_bytes(b"fixture")
    return registry


def _deep_output() -> dict:
    return {
        "run_id": "run-fixture",
        "answer": "Risposta verificata.",
        "claims": [{"text": "Fatto verificato.", "citation_ids": ["S1"]}],
        "citations": [{"source_id": "S1", "url": "https://example.org/source"}],
        "sources": [{"source_id": "S1", "url": "https://example.org/source"}],
        "steps": 2,
        "partial": False,
        "errors": [],
        "network_mode": "read_only",
        "trace_path": "/state/run-fixture.jsonl",
    }


def test_orchestrator_can_answer_without_tool(tmp_path: Path) -> None:
    registry, _ = _registry(tmp_path)
    provider = SequenceProvider(
        ['{"action":"respond","response":"Ciao, come posso aiutarti?","tool_id":null,"arguments":{}}']
    )
    manager = ModelToolManager(registry, runner=lambda *args: (_ for _ in ()).throw(AssertionError()))

    result = ModelToolOrchestrator(provider, manager, registry).run("Ciao")

    assert result.response == "Ciao, come posso aiutarti?"
    assert result.provenance.tools_executed is False
    assert result.metadata["tool_decision"] == "respond"


def test_orchestrator_invokes_one_typed_tool_and_returns_provenance(tmp_path: Path) -> None:
    registry, spec = _registry(tmp_path)
    provider = SequenceProvider(
        [
            '{"action":"tool","response":null,"tool_id":"semantic_v1","arguments":{"query":"scadenza","documents":[{"document_id":"d1","text":"Scadenza 31 luglio"}]}}',
            "La scadenza trovata è il 31 luglio.",
        ]
    )
    manager = ModelToolManager(
        registry,
        runner=lambda selected, snapshot, payload: {"ranked": [{"document_id": "d1", "score": 1.0}]},
        resource_probe=lambda selected: ResourceDecision(ok=True, reason="fixture"),
    )

    result = ModelToolOrchestrator(provider, manager, registry).run("Trova la scadenza")

    assert result.response == "La scadenza trovata è il 31 luglio."
    assert result.provenance.tools_executed is True
    assert result.provenance.command_count == 1
    assert result.provenance.execution_evidence[0].command == f"model_tool:{spec.tool_id}"
    assert result.tool_results[0].ok is True
    assert len(provider.calls) == 2
    assert [message["role"] for message in provider.calls[1]] == ["system", "user"]
    assert "<model_tool_result>" in provider.calls[1][1]["content"]


def test_invalid_tool_decision_is_not_executed(tmp_path: Path) -> None:
    registry, _ = _registry(tmp_path)
    provider = SequenceProvider(["Risposta naturale non JSON"])
    called = []
    manager = ModelToolManager(registry, runner=lambda *args: called.append(args))

    result = ModelToolOrchestrator(provider, manager, registry).run("Ciao")

    assert result.response == "Risposta naturale non JSON"
    assert result.metadata["tool_decision"] == "invalid"
    assert result.provenance.command_count == 0
    assert called == []


def test_schema_diagnostic_identifies_exact_extra_field_without_value(tmp_path: Path) -> None:
    registry, _ = _registry(tmp_path)
    provider = SequenceProvider([
        '{"action":"respond","response":"No tool","tool_id":null,"arguments":{},"write":false}'
    ])
    result = ModelToolOrchestrator(provider, ModelToolManager(registry), registry).run("Ciao")
    diagnostic = result.metadata["tool_decision_diagnostic"]
    assert diagnostic["reason"] == "tool_decision_schema_invalid"
    assert diagnostic["validation_errors"] == [{"field": "write", "type": "extra_forbidden"}]
    assert "false" not in str(diagnostic)


def test_capability_action_dispatches_deep_research_once(tmp_path: Path) -> None:
    registry = _deep_registry(tmp_path)
    provider = SequenceProvider(
        [
            '{"action":"deep_web_research","capability":"deep_web_research",'
            '"tool":"deep_web_research_agentcpm_v1",'
            '"tool_id":"deep_web_research_agentcpm_v1",'
            '"arguments":{"query":"ricerca fixture","max_steps":3,"max_sources":99}}'
        ]
    )
    calls = []

    def runner(selected, snapshot, payload):
        calls.append(payload)
        return _deep_output()

    manager = ModelToolManager(
        registry,
        runner=runner,
        resource_probe=lambda selected: ResourceDecision(ok=True, reason="fixture"),
        gpu_session_factory=lambda selected, tool_id: nullcontext({"enabled": False}),
    )

    result = ModelToolOrchestrator(provider, manager, registry).run("Ricerca approfondita")

    assert len(calls) == 1
    assert calls[0]["max_steps"] == 10
    assert calls[0]["max_sources"] == 30
    assert len(provider.calls) == 1
    assert result.tool_results[0].output["run_id"] == "run-fixture"
    assert result.metadata["final_render"] == "deterministic_tool_answer"
    assert "Risposta verificata." in result.response
    assert "Fatto verificato." in result.response
    assert "https://example.org/source" in result.response
    assert '"action"' not in result.response


def test_runtime_truncated_decision_is_repaired_and_dispatched_once(tmp_path: Path) -> None:
    registry = _deep_registry(tmp_path)
    provider = SequenceProvider([RUNTIME_TRUNCATED_DECISION])
    calls = []

    def runner(selected, snapshot, payload):
        calls.append(payload)
        return _deep_output()

    manager = ModelToolManager(
        registry,
        runner=runner,
        resource_probe=lambda selected: ResourceDecision(ok=True, reason="fixture"),
        gpu_session_factory=lambda selected, tool_id: nullcontext({"enabled": False}),
    )

    result = ModelToolOrchestrator(provider, manager, registry).run(
        "Fai una ricerca approfondita sulla quantizzazione."
    )

    assert len(calls) == 1
    assert calls[0] == {
        "query": "quantizzazione",
        "max_sources": 8,
        "max_steps": 10,
    }
    assert len(provider.calls) == 1
    assert result.tool_results[0].output["run_id"] == "run-fixture"
    assert '"action"' not in result.response


def test_runtime_extra_arguments_are_normalized_before_dispatch(tmp_path: Path) -> None:
    registry = _deep_registry(tmp_path)
    provider = SequenceProvider(
        [
            '{"action":"tool","response":null,'
            '"tool_id":"deep_web_research_agentcpm_v1",'
            '"arguments":{"query":"quantizzazione modelli linguistici tecniche quantization '
            'quantization LLM GPT-4o QLoRA","max_sources":5,"max_steps":3,'
            '"domains":[],"seed_urls":[],"language":"it"}}'
        ]
    )
    calls = []
    manager = ModelToolManager(
        registry,
        runner=lambda selected, snapshot, payload: calls.append(payload) or _deep_output(),
        resource_probe=lambda selected: ResourceDecision(ok=True, reason="fixture"),
        gpu_session_factory=lambda selected, tool_id: nullcontext({"enabled": False}),
    )

    result = ModelToolOrchestrator(provider, manager, registry).run("Ricerca approfondita")

    assert len(calls) == 1
    assert calls[0]["max_steps"] == 10
    assert calls[0]["max_sources"] == 8
    assert "language" not in calls[0]
    assert result.tool_results[0].ok is True


def test_deep_research_receives_complete_original_request(tmp_path: Path) -> None:
    registry = _deep_registry(tmp_path)
    provider = SequenceProvider(
        [
            '{"action":"tool","response":null,'
            '"tool_id":"deep_web_research_agentcpm_v1",'
            '"arguments":{"query":"atlas maps","max_sources":8,"max_steps":10}}'
        ]
    )
    calls = []
    manager = ModelToolManager(
        registry,
        runner=lambda selected, snapshot, payload: calls.append(payload) or _deep_output(),
        resource_probe=lambda selected: ResourceDecision(ok=True, reason="fixture"),
        gpu_session_factory=lambda selected, tool_id: nullcontext({"enabled": False}),
    )
    message = (
        "Research atlas compiler allocation runtime limits, benefits, and methods."
    )

    result = ModelToolOrchestrator(provider, manager, registry).run(message)

    assert len(calls) == 1
    assert calls[0]["query"] == message
    assert result.tool_results[0].ok is True


def test_invalid_control_action_is_not_exposed_or_executed(tmp_path: Path) -> None:
    registry, _ = _registry(tmp_path)
    control_json = (
        '{"action":"deep_web_research","tool_id":"semantic_v1",'
        '"arguments":{"query":"fixture"}}'
    )
    provider = SequenceProvider([control_json])
    called = []
    manager = ModelToolManager(registry, runner=lambda *args: called.append(args))

    result = ModelToolOrchestrator(provider, manager, registry).run("Ricerca")

    assert result.response.startswith(
        "Richiesta tool non eseguita: tool_decision_identity_mismatch "
    )
    assert control_json not in result.response
    assert "fixture" not in result.response
    assert result.metadata["tool_decision"] == "invalid"
    diagnostic = result.metadata["tool_decision_diagnostic"]
    assert diagnostic["reason"] == "tool_decision_identity_mismatch"
    assert diagnostic["redacted_payload"]["action"] == "deep_web_research"
    assert diagnostic["redacted_payload"]["tool_id"] == "semantic_v1"
    assert diagnostic["redacted_payload"]["argument_keys"] == ["query"]
    assert "fixture" not in str(diagnostic)
    assert result.tool_results == ()
    assert called == []


def test_deep_research_failure_is_explicit_and_not_retried(tmp_path: Path) -> None:
    registry = _deep_registry(tmp_path)
    provider = SequenceProvider(
        [
            '{"action":"deep_web_research","tool_id":"deep_web_research_agentcpm_v1",'
            '"arguments":{"query":"ricerca fixture"}}'
        ]
    )
    calls = []

    def runner(selected, snapshot, payload):
        calls.append(payload)
        raise RuntimeError("fixture_failure")

    manager = ModelToolManager(
        registry,
        runner=runner,
        resource_probe=lambda selected: ResourceDecision(ok=True, reason="fixture"),
        gpu_session_factory=lambda selected, tool_id: nullcontext({"enabled": False}),
    )

    result = ModelToolOrchestrator(provider, manager, registry).run("Ricerca approfondita")

    assert len(calls) == 1
    assert len(provider.calls) == 1
    assert result.response == "Ricerca approfondita non completata: tool_runtime_error."
    assert result.tool_results[0].ok is False
    assert result.tool_results[0].error_type == "tool_runtime_error"
    assert '"action"' not in result.response


def test_unknown_tool_is_reported_to_orchestrator_without_fallback(tmp_path: Path) -> None:
    registry, _ = _registry(tmp_path)
    provider = SequenceProvider(
        [
            '{"action":"tool","response":null,"tool_id":"missing","arguments":{}}',
            "Lo strumento richiesto non è disponibile.",
        ]
    )
    manager = ModelToolManager(registry)

    result = ModelToolOrchestrator(provider, manager, registry).run("Usa missing")

    assert result.response == "Lo strumento richiesto non è disponibile."
    assert result.tool_results[0].error_type == "tool_unavailable"
    assert result.metadata["tool_ok"] is False


def test_strict_decision_repairs_only_bare_json_keys() -> None:
    decision = _strict_decision(
        '{"action":"tool","response":null,"tool_id":"deep_web_research_agentcpm_v1","arguments":{"query":"logs",max_steps:8,max_sources:8,"seed_urls":[]}}'
    )
    assert decision.action == "tool"
    assert decision.arguments["max_steps"] == 8
    assert decision.arguments["max_sources"] == 8


def test_web_research_normalization_enforces_research_budget() -> None:
    from ralfloop_agent.model_tools.orchestrator import _normalize_tool_arguments

    result = _normalize_tool_arguments(
        "deep_web_research_agentcpm_v1",
        {"query": "Fai una ricerca approfondita sugli ETF quality", "max_steps": 3, "max_sources": 5},
    )
    assert result["max_steps"] == 10
    assert result["max_sources"] == 8


def test_log_research_normalization_preserves_small_budget() -> None:
    from ralfloop_agent.model_tools.orchestrator import _normalize_tool_arguments

    result = _normalize_tool_arguments(
        "deep_web_research_agentcpm_v1",
        {"query": "Controlla i log recenti per errori", "max_steps": 3, "max_sources": 5},
    )
    assert result["max_steps"] == 3
    assert result["max_sources"] == 5


def test_deep_research_renderer_includes_supported_claims_and_sources() -> None:
    from ralfloop_agent.model_tools.manager import ModelToolEnvelope
    from ralfloop_agent.model_tools.orchestrator import _render_deep_research

    envelope = ModelToolEnvelope(
        ok=True,
        tool_id="deep_web_research_agentcpm_v1",
        model_id="fixture/research",
        revision="rev1",
        device="cpu",
        duration_ms=1,
        input_hash="a" * 64,
        output={
            "answer": "ETF quality: analisi del fattore qualità",
            "claims": [
                {
                    "text": "Il fattore qualità seleziona imprese con redditività elevata, utili stabili e leva contenuta.",
                    "citation_ids": ["S1", "S2"],
                },
                {
                    "text": "I risultati dipendono dalla metodologia dell'indice e dalle valutazioni iniziali.",
                    "citation_ids": ["S2"],
                },
            ],
            "citations": [
                {"source_id": "S1", "url": "https://example.org/one"},
                {"source_id": "S2", "url": "https://example.org/two"},
            ],
        },
    )

    rendered = _render_deep_research(envelope)
    assert "ETF quality: analisi del fattore qualità" in rendered
    assert "redditività elevata" in rendered
    assert "[S1; S2]" in rendered
    assert "https://example.org/one" in rendered
    assert "https://example.org/two" in rendered
