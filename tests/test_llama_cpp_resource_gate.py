from __future__ import annotations

from pathlib import Path

import pytest
import requests

from ralfloop_agent.providers.llama_cpp_server import (
    LlamaCppServerConfig,
    LlamaCppServerManager,
    LlamaCppServerUnavailable,
    classify_gpu_resource_state,
)


class Response:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload

    def close(self):
        return None


class Session:
    def __init__(self, *items):
        self.items = list(items)

    def get(self, url, **kwargs):
        item = self.items.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def config(tmp_path: Path) -> LlamaCppServerConfig:
    return LlamaCppServerConfig(
        model_path=tmp_path / "model.gguf",
        model_hash="0" * 64,
        server_bin=tmp_path / "llama-server",
        state_dir=tmp_path / "state",
    )


def classify(**overrides):
    values = {
        "ollama_server_running": True,
        "ollama_models": [],
        "gpu_free_mib": 7425,
        "gpu_processes": [],
        "llama_cpp_running": False,
    }
    values.update(overrides)
    return classify_gpu_resource_state(**values)


def observer(*processes, free_mib=7425):
    return lambda: {"free_mib": free_mib, "processes": list(processes)}


def test_ollama_server_absent_is_distinct_from_loaded_model():
    status = classify(ollama_server_running=False)
    assert status["ollama_server_running"] is False
    assert status["ollama_model_loaded"] is False
    assert status["gate_reason"] == "gpu_available"


def test_ollama_server_present_but_idle_is_allowed():
    status = classify()
    assert status["ollama_server_running"] is True
    assert status["ollama_model_loaded"] is False
    assert status["gate_reason"] == "gpu_available"


def test_empty_api_ps_is_current_empty_state(tmp_path):
    manager = LlamaCppServerManager(
        config(tmp_path), session=Session(Response({"models": []})), gpu_observer=observer()
    )
    assert manager.resource_gate_status()["gate_reason"] == "gpu_available"


def test_loaded_ollama_model_with_vram_blocks():
    status = classify(ollama_models=[{"name": "qwen", "size_vram": 1024}])
    assert status["ollama_model_loaded"] is True
    assert status["ollama_gpu_memory_in_use"] is True
    assert status["gate_reason"] == "ollama_model_already_loaded"


def test_ollama_runner_with_vram_is_identified():
    status = classify(
        ollama_models=[{"name": "qwen", "size_vram": 1024}],
        gpu_processes=[{"pid": 41, "process_name": "/usr/bin/ollama", "used_gpu_memory_mib": 512}],
    )
    assert status["ollama_runner_pids"] == [41]
    assert status["ollama_gpu_memory_bytes"] == 1024


def test_stale_previous_state_is_not_cached(tmp_path):
    manager = LlamaCppServerManager(
        config(tmp_path),
        session=Session(
            Response({"models": [{"name": "old", "size_vram": 100}]}),
            Response({"models": []}),
        ),
        gpu_observer=observer(),
    )
    assert manager.resource_gate_status()["gate_reason"] == "ollama_model_already_loaded"
    assert manager.resource_gate_status()["gate_reason"] == "gpu_available"


def test_ollama_api_unreachable_fails_closed(tmp_path):
    manager = LlamaCppServerManager(
        config(tmp_path), session=Session(requests.ConnectionError("down")), gpu_observer=observer()
    )
    with pytest.raises(LlamaCppServerUnavailable, match="ollama_gpu_state_unavailable"):
        manager.resource_gate_status()


def test_ambiguous_ollama_residency_fails_closed():
    status = classify(ollama_models=[{"name": "qwen"}])
    assert status["ambiguous"] is True
    assert status["gate_reason"] == "gpu_resource_state_ambiguous"


def test_llama_cpp_already_running_is_distinct():
    status = classify(llama_cpp_running=True)
    assert status["llama_cpp_running"] is True
    assert status["gate_reason"] == "llama_cpp_already_running"


def test_foreign_gpu_process_is_observed_without_false_ollama_reason():
    status = classify(
        gpu_processes=[{"pid": 51, "process_name": "ffmpeg", "used_gpu_memory_mib": 100}]
    )
    assert status["foreign_gpu_process_present"] is True
    assert status["foreign_gpu_pids"] == [51]
    assert status["gate_reason"] == "gpu_available"


def test_free_vram_is_reported():
    status = classify(gpu_free_mib=7000)
    assert status["gpu_available"] is True
    assert status["gpu_free_mib"] == 7000


def test_cpu_only_ollama_model_does_not_emit_loaded_gpu_reason():
    status = classify(ollama_models=[{"name": "gemma4", "size_vram": 0}])
    assert status["ollama_model_loaded"] is True
    assert status["ollama_gpu_memory_in_use"] is False
    assert status["gate_reason"] == "gpu_available"
