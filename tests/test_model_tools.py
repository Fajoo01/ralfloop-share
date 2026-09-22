from __future__ import annotations

import json
from contextlib import contextmanager
from pathlib import Path
import subprocess
import sys

import pytest
from pydantic import ValidationError

import ralfloop_agent.model_tools.manager as model_tool_manager
from ralfloop_agent.model_tools import (
    ModelToolManager,
    ModelToolRegistry,
    ModelToolSpec,
    ResourceDecision,
    route_model_tool,
)
from ralfloop_agent.model_tools.manager import _isolated_environment, _terminate_worker_group, _worker_command
from ralfloop_agent.model_tools.worker import _extraction_template
from ralfloop_agent.model_tools.orchestrator import _normalize_tool_arguments


CONFIG = Path("config/model_tools.json")


def _schemas(capability: str) -> tuple[dict, dict]:
    if capability == "deep_web_research":
        return (
            {"type": "object", "required": ["query"], "additionalProperties": False, "properties": {"query": {"type": "string"}}},
            {"type": "object", "required": ["answer"], "additionalProperties": False, "properties": {"answer": {"type": "string"}}},
        )
    if capability == "document_to_markdown":
        return (
            {"type": "object", "required": ["file_path"], "additionalProperties": False, "properties": {"file_path": {"type": "string"}}},
            {"type": "object", "required": ["markdown"], "additionalProperties": False, "properties": {"markdown": {"type": "string"}}},
        )
    if capability == "extract_structured_data":
        return (
            {"type": "object", "required": ["text", "json_schema"], "additionalProperties": False, "properties": {"text": {"type": "string"}, "json_schema": {"type": "object"}}},
            {"type": "object", "required": ["data"], "additionalProperties": False, "properties": {"data": {"type": "object"}}},
        )
    if capability in {"semantic_search", "rerank_documents", "retrieve_code_context"}:
        return (
            {"type": "object", "required": ["query", "documents"], "additionalProperties": False, "properties": {"query": {"type": "string"}, "documents": {"type": "array"}}},
            {"type": "object", "required": ["ranked"], "additionalProperties": False, "properties": {"ranked": {"type": "array"}}},
        )
    if capability == "verify_claim_support":
        return (
            {"type": "object", "required": ["premise", "hypothesis"], "additionalProperties": False, "properties": {"premise": {"type": "string"}, "hypothesis": {"type": "string"}}},
            {"type": "object", "required": ["label", "scores", "consultative"], "additionalProperties": False, "properties": {"label": {"type": "string"}, "scores": {"type": "object"}, "consultative": {"type": "boolean"}}},
        )
    return (
        {"type": "object", "required": ["text", "labels"], "additionalProperties": False, "properties": {"text": {"type": "string"}, "labels": {"type": "array"}}},
        {"type": "object", "required": ["entities"], "additionalProperties": False, "properties": {"entities": {"type": "array"}}},
    )


def _spec(tmp_path: Path, capability: str = "semantic_search", **overrides) -> ModelToolSpec:
    input_schema, output_schema = _schemas(capability)
    values = {
        "tool_id": f"{capability}_v1",
        "capability": capability,
        "model_id": f"fixture/{capability}",
        "revision": "fixture-revision",
        "backend": "fixture",
        "python_executable": sys.executable,
        "device": "cpu",
        "timeout_sec": 2,
        "max_ram_mb": 1,
        "max_vram_mb": 0,
        "local_files_only": True,
        "trust_remote_code": False,
        "enabled": True,
        "status": "ready",
        "model_card_status": "fixture",
        "license": "fixture",
        "input_schema": input_schema,
        "output_schema": output_schema,
    }
    values.update(overrides)
    spec = ModelToolSpec.model_validate(values)
    snapshot = tmp_path / ("models--" + spec.model_id.replace("/", "--")) / "snapshots" / spec.revision
    snapshot.mkdir(parents=True, exist_ok=True)
    (snapshot / "model.safetensors").write_bytes(b"fixture")
    return spec


def _registry(tmp_path: Path, specs: list[ModelToolSpec]) -> ModelToolRegistry:
    return ModelToolRegistry(specs, cache_root=tmp_path)


def _ok_resources(spec: ModelToolSpec) -> ResourceDecision:
    return ResourceDecision(ok=True, reason="fixture", ram_available_mb=99999)


def test_versioned_registry_contains_first_four_typed_tools(tmp_path: Path) -> None:
    registry = ModelToolRegistry.load(CONFIG, cache_root=tmp_path)
    assert registry.schema_version == "1.0"
    assert {spec.capability for spec in registry.specs.values()} == {
        "document_to_markdown",
        "extract_structured_data",
        "semantic_search",
        "rerank_documents",
        "verify_claim_support",
        "extract_entities",
        "retrieve_code_context",
        "sandboxed_remote_code",
        "deep_web_research",
    }
    assert {spec.tool_id for spec in registry.specs.values() if spec.enabled} == {
        "extract_structured_data_v1",
        "semantic_retriever_bge_m3_v1",
        "document_reranker_v1",
        "deep_web_research_agentcpm_v1",
        "sandboxed_remote_code",
    }
    assert registry.get("document_to_markdown_v1").status == "runtime_cpu_timeout"
    assert all(spec.revision for spec in registry.specs.values())
    assert all(spec.local_files_only is True for spec in registry.specs.values())
    assert all(spec.trust_remote_code is False for spec in registry.specs.values())


def test_json_schema_is_converted_to_nuextract_template() -> None:
    assert _extraction_template(
        {
            "type": "object",
            "properties": {
                "titolo": {"type": "string"},
                "budget": {"type": "number"},
                "beneficiari": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["titolo"],
        }
    ) == {"titolo": "", "budget": None, "beneficiari": [""]}


def test_enabled_tool_requires_pinned_revision(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="requires_revision"):
        _spec(tmp_path, revision=None)


def test_missing_snapshots_are_reported_without_download(tmp_path: Path) -> None:
    registry = ModelToolRegistry.load(CONFIG, cache_root=tmp_path)
    health = registry.health()
    statuses = {row["tool_id"]: row["status"] for row in health["tools"]}
    assert statuses["code_retriever_v1"] == "sandboxed_remote_code_required"
    assert statuses["sandboxed_remote_code"] == "ready"
    assert set(statuses.values()) == {"snapshot_missing", "sandboxed_remote_code_required", "ready"}
    assert all("<PINNED_REVISION>" not in row["download_command"] for row in health["tools"])


def test_remote_code_required_model_is_routed_to_sandbox_tool(tmp_path: Path) -> None:
    spec = _spec(tmp_path, trust_remote_code=True)
    status = _registry(tmp_path, [spec]).availability(spec)
    assert status.status == "sandboxed_remote_code_required"


def test_registry_policy_rejects_known_remote_code_model_without_enabling_trust(tmp_path: Path) -> None:
    spec = _spec(
        tmp_path,
        enabled=False,
        status="sandboxed_remote_code_required",
        trust_remote_code=False,
    )
    status = _registry(tmp_path, [spec]).availability(spec)
    assert status.status == "sandboxed_remote_code_required"


def test_dependency_missing_is_reported_after_snapshot_detection(tmp_path: Path) -> None:
    spec = _spec(tmp_path, enabled=False, status="dependency_missing")
    status = _registry(tmp_path, [spec]).availability(spec)
    assert status.status == "dependency_missing"
    assert status.path is not None
    assert status.safetensors is True


def test_model_tool_is_lazy_and_returns_uniform_envelope(tmp_path: Path) -> None:
    spec = _spec(tmp_path)
    calls = []
    audits = []

    def runner(selected, snapshot, payload):
        calls.append((selected.tool_id, snapshot, payload))
        return {"ranked": [{"document_id": "d1", "score": 1.0}]}

    manager = ModelToolManager(
        _registry(tmp_path, [spec]),
        runner=runner,
        resource_probe=_ok_resources,
        audit=lambda event, fields: audits.append(event),
    )
    assert calls == []
    result = manager.invoke(spec.tool_id, {"query": "q", "documents": []})
    assert result.ok is True
    assert result.tool_id == spec.tool_id
    assert result.revision == "fixture-revision"
    assert result.provenance["runtime"] == "isolated_process_per_call"
    assert len(result.input_hash) == 64
    assert len(calls) == 1
    assert "model_tool_unloaded" in audits


def test_resource_gate_blocks_before_load(tmp_path: Path) -> None:
    spec = _spec(tmp_path)
    calls = []
    manager = ModelToolManager(
        _registry(tmp_path, [spec]),
        runner=lambda *args: calls.append(args),
        resource_probe=lambda selected: ResourceDecision(ok=False, reason="ram_gate_failed"),
    )
    result = manager.invoke(spec.tool_id, {"query": "q", "documents": []})
    assert result.ok is False
    assert result.error_type == "ram_gate_failed"
    assert calls == []


def test_agentcpm_gpu_handoff_precedes_resource_probe_and_runner(tmp_path: Path) -> None:
    spec = _spec(
        tmp_path,
        capability="deep_web_research",
        backend="agentcpm_llama_cpp",
        device="cuda",
        max_vram_mb=6000,
    )
    events = []

    @contextmanager
    def gpu_session(selected, tool_id):
        events.append(("handoff_enter", selected.tool_id, tool_id))
        yield {"enabled": True}
        events.append(("handoff_exit", selected.tool_id, tool_id))

    def probe(selected):
        events.append(("resource_probe", selected.tool_id))
        return ResourceDecision(ok=True, reason="resource_gate_passed", vram_available_mb=7425)

    def runner(selected, snapshot, payload):
        events.append(("runner", selected.tool_id))
        return {"answer": "verified"}

    manager = ModelToolManager(
        _registry(tmp_path, [spec]),
        runner=runner,
        resource_probe=probe,
        gpu_session_factory=gpu_session,
    )
    result = manager.invoke(spec.tool_id, {"query": "verify"})

    assert result.ok is True
    assert [row[0] for row in events] == ["handoff_enter", "resource_probe", "runner", "handoff_exit"]
    assert result.provenance["gpu_handoff"] is True
    assert result.provenance["storage_mode"] == "mmap_ssd"


def test_resident_agentcpm_reuse_skips_duplicate_handoff_and_vram_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec = _spec(
        tmp_path,
        capability="deep_web_research",
        backend="agentcpm_llama_cpp",
        device="cuda",
        max_vram_mb=6000,
    )
    monkeypatch.setenv("RALF_MODEL_TOOLS_ALLOW_CUDA", "1")
    monkeypatch.setattr(model_tool_manager, "_resident_agentcpm_ready", lambda selected: True)

    decision = model_tool_manager._resource_probe(spec)
    assert decision.ok is True
    assert decision.reason == "resident_agentcpm_reuse"

    with model_tool_manager._gpu_session(spec, spec.tool_id) as state:
        assert state == {"enabled": False, "resident_agentcpm": True}


def test_document_tool_rejects_path_outside_allowlist(tmp_path: Path, monkeypatch) -> None:
    spec = _spec(tmp_path, capability="document_to_markdown")
    forbidden = tmp_path / "outside.pdf"
    forbidden.write_bytes(b"pdf")
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    monkeypatch.setenv("RALF_MODEL_TOOL_DOCUMENT_ROOTS", str(allowed))
    calls = []
    manager = ModelToolManager(
        _registry(tmp_path, [spec]),
        runner=lambda *args: calls.append(args),
        resource_probe=_ok_resources,
    )

    result = manager.invoke(spec.tool_id, {"file_path": str(forbidden)})

    assert result.error_type == "document_path_forbidden"
    assert calls == []


def test_timeout_and_circuit_breaker(tmp_path: Path) -> None:
    spec = _spec(tmp_path)

    def timeout(*args):
        raise subprocess.TimeoutExpired("worker", 1)

    manager = ModelToolManager(
        _registry(tmp_path, [spec]),
        runner=timeout,
        resource_probe=_ok_resources,
        circuit_threshold=2,
    )
    payload = {"query": "q", "documents": []}
    assert manager.invoke(spec.tool_id, payload).error_type == "tool_timeout"
    assert manager.invoke(spec.tool_id, payload).error_type == "tool_timeout"
    assert manager.invoke(spec.tool_id, payload).error_type == "circuit_open"


def test_worker_environment_is_offline_and_has_no_credentials(tmp_path: Path, monkeypatch) -> None:
    spec = _spec(tmp_path)
    monkeypatch.setenv("HF_TOKEN", "secret")
    monkeypatch.setenv("HTTP_PROXY", "http://secret")
    env = _isolated_environment(spec)
    assert env["HF_HUB_OFFLINE"] == "1"
    assert env["TRANSFORMERS_OFFLINE"] == "1"
    assert "HF_TOKEN" not in env
    assert "HTTP_PROXY" not in env


def test_deep_web_worker_receives_only_allowlisted_runtime_settings(tmp_path: Path, monkeypatch) -> None:
    spec = _spec(tmp_path, capability="deep_web_research")
    monkeypatch.setenv("RALFLOOP_SEARXNG_URL", "http://127.0.0.1:8889")
    monkeypatch.setenv("RALF_AGENTCPM_LLAMA_SERVER_BIN", "/safe/llama-server")
    monkeypatch.setenv("RALF_MODEL_TOOL_STATE_DIR", "/safe/state")
    monkeypatch.setenv("HF_TOKEN", "secret")

    env = _isolated_environment(spec)

    assert env["RALFLOOP_SEARXNG_URL"] == "http://127.0.0.1:8889"
    assert env["RALF_AGENTCPM_LLAMA_SERVER_BIN"] == "/safe/llama-server"
    assert env["RALF_MODEL_TOOL_STATE_DIR"] == "/safe/state"
    assert "HF_TOKEN" not in env


def test_worker_command_keeps_isolation_and_imports_current_release(tmp_path: Path) -> None:
    spec = _spec(tmp_path)
    command = _worker_command(spec)
    assert command[:3] == [spec.python_executable, "-I", "-c"]
    assert "sys.path.insert" in command[3]
    assert "ralfloop_agent.model_tools.worker" in command[3]
    assert "-m" not in command


def test_interrupted_worker_group_is_terminated_killed_and_reaped(monkeypatch) -> None:
    class Process:
        pid = 4242

        def __init__(self):
            self.waits = [subprocess.TimeoutExpired("worker", 1), -9]

        def poll(self):
            return None

        def wait(self, timeout=None):
            result = self.waits.pop(0)
            if isinstance(result, BaseException):
                raise result
            return result

    signals = []
    monkeypatch.setattr("ralfloop_agent.model_tools.manager.os.killpg", lambda pid, sig: signals.append((pid, sig)))
    process = Process()

    _terminate_worker_group(process, timeout=1)

    assert signals == [(4242, 15), (4242, 9)]
    assert process.waits == []


def _fixture_runner(spec: ModelToolSpec, snapshot: Path, payload: dict) -> dict:
    if spec.capability in {"semantic_search", "rerank_documents", "retrieve_code_context"}:
        query_words = set(payload["query"].casefold().split())
        ranked = []
        for item in payload["documents"]:
            score = len(query_words & set(item["text"].casefold().split()))
            ranked.append({"document_id": item["document_id"], "score": float(score)})
        return {"ranked": sorted(ranked, key=lambda row: row["score"], reverse=True)}
    if spec.capability == "verify_claim_support":
        hypothesis = payload["hypothesis"].casefold()
        label = "contradiction" if "non " in hypothesis else "entailment" if hypothesis in payload["premise"].casefold() else "neutral"
        return {"label": label, "scores": {label: 1.0}, "consultative": True}
    return {
        "entities": [
            {"text": "Ada", "label": "persona"},
            {"text": "ACME", "label": "organizzazione"},
            {"text": "17 luglio 2026", "label": "data"},
            {"text": "100 euro", "label": "importo"},
        ]
    }


@pytest.mark.parametrize(
    ("capability", "payload", "assertion"),
    (
        (
            "semantic_search",
            {"query": "documento corretto", "documents": [{"document_id": f"d{i}", "text": "irrilevante"} for i in range(9)] + [{"document_id": "target", "text": "documento corretto"}]},
            lambda output: output["ranked"][0]["document_id"] == "target",
        ),
        (
            "rerank_documents",
            {"query": "fonte pertinente", "documents": [{"document_id": "x", "text": "altro"}, {"document_id": "target", "text": "fonte pertinente"}]},
            lambda output: output["ranked"][0]["document_id"] == "target",
        ),
        (
            "verify_claim_support",
            {"premise": "Roma è in Italia", "hypothesis": "Roma è in Italia"},
            lambda output: output["label"] == "entailment" and output["consultative"] is True,
        ),
        (
            "extract_entities",
            {"text": "Ada di ACME il 17 luglio 2026 pagò 100 euro", "labels": ["persona", "organizzazione", "data", "importo"]},
            lambda output: {item["label"] for item in output["entities"]} == {"persona", "organizzazione", "data", "importo"},
        ),
        (
            "retrieve_code_context",
            {"query": "trigger goal", "documents": [{"document_id": "other", "text": "unrelated"}, {"document_id": "router", "text": "trigger goal"}]},
            lambda output: output["ranked"][0]["document_id"] == "router",
        ),
    ),
)
def test_specialist_model_tool_mock_canaries(tmp_path: Path, capability, payload, assertion) -> None:
    spec = _spec(tmp_path, capability)
    manager = ModelToolManager(
        _registry(tmp_path, [spec]),
        runner=_fixture_runner,
        resource_probe=_ok_resources,
    )
    result = manager.invoke(spec.tool_id, payload)
    assert result.ok is True
    assert assertion(result.output)


def test_model_tool_routing_has_no_silent_fallback(tmp_path: Path) -> None:
    registry = ModelToolRegistry.load(CONFIG, cache_root=tmp_path)
    route = route_model_tool("Esegui una ricerca semantica", registry)
    assert route.capability == "semantic_search"
    assert route.status == "tool_unavailable"
    assert route.reason == "snapshot_missing"


def test_deep_web_research_routes_explicit_natural_request_without_silent_fallback(tmp_path: Path) -> None:
    registry = ModelToolRegistry.load(CONFIG, cache_root=tmp_path)
    route = route_model_tool("Fai una ricerca web approfondita con fonti", registry)
    assert route.capability == "deep_web_research"
    assert route.tool_id == "deep_web_research_agentcpm_v1"
    assert route.status == "tool_unavailable"
    assert route.reason == "snapshot_missing"


def test_deep_web_research_routes_safe_log_inspection(tmp_path: Path) -> None:
    registry = ModelToolRegistry.load(CONFIG, cache_root=tmp_path)
    route = route_model_tool("Controlla i log del backend", registry)
    assert route.capability == "deep_web_research"
    assert route.tool_id == "deep_web_research_agentcpm_v1"


def test_remote_code_request_routes_only_to_sandboxed_builtin(tmp_path: Path) -> None:
    registry = ModelToolRegistry.load(CONFIG, cache_root=tmp_path)
    route = route_model_tool("Analizza codice remoto", registry)

    assert route.capability == "sandboxed_remote_code"
    assert route.tool_id == "sandboxed_remote_code"
    assert route.status == "ready"
    assert route.reason == "enabled_builtin_sandbox"


def test_nli_mock_covers_entailment_contradiction_and_neutral(tmp_path: Path) -> None:
    spec = _spec(tmp_path, "verify_claim_support")
    manager = ModelToolManager(
        _registry(tmp_path, [spec]),
        runner=_fixture_runner,
        resource_probe=_ok_resources,
    )
    premise = "Roma è in Italia"
    labels = {
        manager.invoke(spec.tool_id, {"premise": premise, "hypothesis": hypothesis}).output["label"]
        for hypothesis in ("Roma è in Italia", "Roma non è in Italia", "Parigi è in Francia")
    }
    assert labels == {"entailment", "contradiction", "neutral"}



def test_deep_research_arguments_drop_unknown_fields() -> None:
    assert _normalize_tool_arguments(
        "deep_web_research_agentcpm_v1",
        {
            "query": "fonti",
            "max_steps": 10,
            "partial": False,
            "network_mode": "read_only",
            "unexpected": "x",
        },
    ) == {
        "query": "fonti",
        "max_steps": 10,
    }


def test_deep_research_arguments_normalize_url_domains_and_seed_hosts() -> None:
    assert _normalize_tool_arguments(
        "deep_web_research_agentcpm_v1",
        {
            "query": "cerca su example.org",
            "domains": [
                "https://huggingface.co/openbmb/AgentCPM-Explore-GGUF",
                "EXAMPLE.ORG/path",
                "invented.invalid",
            ],
            "seed_urls": ["https://huggingface.co/openbmb/AgentCPM-Explore-GGUF"],
        },
    ) == {
        "query": "cerca su example.org",
        "domains": ["huggingface.co", "example.org"],
        "seed_urls": ["https://huggingface.co/openbmb/AgentCPM-Explore-GGUF"],
    }


def test_deep_research_drops_model_invented_domain_restrictions() -> None:
    assert _normalize_tool_arguments(
        "deep_web_research_agentcpm_v1",
        {
            "query": "Fai una ricerca approfondita sugli ETF quality",
            "domains": ["wikipedia.org", "investing.com", "example.org"],
            "max_steps": 10,
        },
    ) == {
        "query": "Fai una ricerca approfondita sugli ETF quality",
        "max_steps": 10,
    }
