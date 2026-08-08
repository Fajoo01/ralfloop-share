from __future__ import annotations

import json
from pathlib import Path

from ralfloop_agent.domains.bandi_runtime_context import (
    ALLOWED_FILES,
    load_bandi_runtime_context,
)
from ralfloop_agent.providers.ollama import DeterministicPlanner, OllamaPlanner


def test_loader_reads_only_fixed_allowlist(tmp_path: Path) -> None:
    for name in ALLOWED_FILES:
        (tmp_path / name).write_text(json.dumps({"name": name}), encoding="utf-8")
    (tmp_path / "secret.json").write_text('{"secret":true}', encoding="utf-8")

    result = load_bandi_runtime_context(tmp_path)

    assert set(result["sources"]) == set(ALLOWED_FILES)
    assert "secret" not in json.dumps(result)
    assert result["application_status"]["name"] == "application_status.json"
    assert result["organization_profile"]["name"] == "organization_profile.json"


def test_loader_reports_missing_without_inventing(tmp_path: Path) -> None:
    result = load_bandi_runtime_context(tmp_path)

    assert "application_status" not in result
    assert all(row["status"] == "missing_or_oversized" for row in result["sources"].values())


def test_bandi_goal_reads_staged_context(monkeypatch) -> None:
    decision = DeterministicPlanner().choose_next_action(
        "Qual è lo stato della candidatura Arianna?", 0
    )
    assert decision.tool_name == "sandbox_read_file"
    assert decision.tool_input == {"path": "bandi_context.json"}

    monkeypatch.setenv("RALF_CHAT_PROVIDER", "llama_cpp")
    planner = OllamaPlanner()
    decision = planner.choose_next_action("Verifica il bando RLD12025048623", 0)
    assert decision.tool_input == {"path": "bandi_context.json"}


def test_bandi_portal_action_routes_to_browser_handoff(monkeypatch) -> None:
    from fastapi.testclient import TestClient
    from openshell_backend.app import app
    from ralfloop_agent.integration.capability_adapter import route_task

    route = route_task("Compila candidatura nel portale Bandi")
    assert route.mode == "external_action"
    assert route.mcp_used == ["browser"]
    assert route.requires_confirmation is False

    monkeypatch.setattr(
        "ralfloop_agent.domains.bandi_runtime_context.load_bandi_runtime_context",
        lambda: {
            "application_status": {
                "call_id": "RLD12025048623",
                "portal_draft_id": "8099217",
                "status": "APPLICATION_DRAFTED",
                "blocking_requirements": ["digital_signature_required"],
            }
        },
    )
    response = TestClient(app).post(
        "/tasks/run",
        json={
            "user_goal": "Compila candidatura nel portale Bandi",
            "extra_context": {
                "terminal_client": {
                    "provider": "llama_cpp",
                    "provider_endpoint": "http://127.0.0.1:19091",
                    "model_id": "qwen3:8b",
                }
            },
        },
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["capability"] == "bandi_browser_fill"
    assert payload["stop_reason"] == "mcp_execution_required"
    assert payload["approval_required"] is False
    action = json.loads(payload["final_answer"])
    assert action["portal_draft_id"] == "8099217"
    assert action["status"] == "mcp_execution_required"
    assert payload["result_envelope"]["provenance"]["tools_executed"] is False
    assert payload["result_envelope"]["provenance"]["command_count"] == 0
