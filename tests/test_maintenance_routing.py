from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from openshell_backend.app import app
from ralfloop_agent.integration.capability_adapter import route_task, route_to_legacy_dict


@pytest.mark.parametrize(
    ("goal", "mode"),
    (
        ("correggi email_ops", "patch_allowed"),
        ("audit pipeline Gmail", "check_only"),
        ("testa MCP", "check_only"),
        ("correggi router Gmail", "patch_allowed"),
        ("verifica /home/bandi/bot-tazzi", "check_only"),
        ("riavvia il browser bridge service in /home/bandi/bot-tazzi", "external_action"),
    ),
)
def test_software_maintenance_never_routes_to_bandi_or_external_connector(goal, mode):
    route = route_task(goal)

    assert route.mode == mode
    assert "local_maintenance" in route.skills_used
    assert "bandi" not in route.skills_used
    assert "bandi_browser_fill" not in route.skills_used
    assert route.mcp_used == []
    legacy = route_to_legacy_dict(route)
    assert legacy["capability"] == "local_software_maintenance"
    assert legacy["canonical_actions_only"] is True
    assert "generic_arbitrary_shell" in legacy["blocked_actions"]


@pytest.mark.parametrize(
    "goal",
    (
        "lo username bandi ha un problema locale",
        "mostra /home/bandi",
        "pathname /home/bandi/bot-tazzi",
    ),
)
def test_username_and_pathname_are_not_grant_intent(goal):
    assert not any(skill.startswith("bandi") for skill in route_task(goal).skills_used)


@pytest.mark.parametrize(
    "goal",
    (
        "Patch local source code in repository worktree only. Do not restart any service.",
        "Fix repository code without restarting the service.",
        "Patch the local worktree; never restart Bot-tazzi.",
    ),
)
def test_negated_restart_keeps_local_code_patch_in_patch_allowed(goal):
    route = route_task(goal)

    assert route.mode == "patch_allowed"
    assert "local_maintenance" in route.skills_used
    assert route.requires_confirmation is False


def test_true_grant_application_keeps_bandi_browser_handoff(monkeypatch):
    monkeypatch.setattr(
        "ralfloop_agent.domains.bandi_runtime_context.load_bandi_runtime_context",
        lambda: {"application_status": {"status": "APPLICATION_DRAFTED"}},
    )
    goal = "Compila candidatura nel portale Bandi"
    route = route_task(goal)

    assert route.mode == "external_action"
    assert "bandi" in route.skills_used
    assert "local_maintenance" not in route.skills_used
    assert route.mcp_used == ["browser"]
    response = TestClient(app).post("/tasks/run", json={"user_goal": goal})
    assert response.status_code == 200
    assert response.json()["capability"] == "bandi_browser_fill"


def test_route_only_exposes_local_maintenance_capability():
    response = TestClient(app).post(
        "/tasks/run",
        json={
            "user_goal": "correggi /home/bandi/bot-tazzi browser_autoloop.py e testa il service",
            "mode": "route_only",
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["capability_route"]["capability"] == "local_software_maintenance"
    assert "bandi" not in payload["capability_route"]["skills_used"]
    assert payload.get("capability") != "bandi_browser_fill"


def test_protected_local_action_uses_canonical_approval_capability(monkeypatch, tmp_path):
    monkeypatch.setenv("RALFLOOP_ENABLE_TELEGRAM_APPROVAL_GATE", "0")
    monkeypatch.setenv("RALFLOOP_TELEGRAM_APPROVAL_DB", str(tmp_path / "approval.sqlite"))
    response = TestClient(app).post(
        "/tasks/run",
        json={"user_goal": "riavvia Bot-tazzi browser bridge"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["capability"] == "local_software_maintenance"
    assert payload["approval_required"] is True
    assert payload["local_maintenance"]["action_id"] == "systemd.restart.bottazzi_browser_bridge"
    assert payload["local_maintenance"]["approval"]["status"] == "approval_gate_disabled"
    assert payload["local_maintenance"]["executed"] is False
    assert payload.get("pending_confirmation_id") is None


def test_local_maintenance_http_surface_rejects_arbitrary_shell():
    client = TestClient(app)
    listed = client.get("/local-maintenance/actions").json()
    rejected = client.get("/local-maintenance/actions/shell.arbitrary/preview").json()

    assert listed["arbitrary_shell"] is False
    assert "shell.arbitrary" not in listed["canonical_actions"]
    assert rejected["status"] == "unsupported_canonical_local_action"


def test_patch_allowed_terminal_cwd_uses_coding_harness(monkeypatch, tmp_path):
    import subprocess

    main = tmp_path / "main"
    worktree = tmp_path / "ticket"
    subprocess.run(["git", "init", "-q", "-b", "main", str(main)], check=True)
    subprocess.run(["git", "-C", str(main), "config", "user.email", "routing-test@example.invalid"], check=True)
    subprocess.run(["git", "-C", str(main), "config", "user.name", "Routing Test"], check=True)
    (main / "base.txt").write_text("base\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(main), "add", "base.txt"], check=True)
    subprocess.run(["git", "-C", str(main), "commit", "-q", "-m", "base"], check=True)
    subprocess.run(["git", "-C", str(main), "worktree", "add", "-q", "-b", "ticket", str(worktree)], check=True)
    observed = {}

    def fake_harness(config):
        observed["workdir"] = str(config.workdir)
        observed["validator"] = config.validator_command
        observed["worker_user"] = config.worker_user
        observed["provider"] = config.provider
        observed["model"] = config.model
        observed["fallback_provider"] = config.fallback_provider
        observed["fallback_model"] = config.fallback_model
        return {"final_status": "pass", "decision": "deterministic_fast_path", "changed_files": []}

    monkeypatch.setattr(
        "ralfloop_agent.coding_harness.harness.run_harness", fake_harness
    )
    monkeypatch.setenv("RALF_CODE_WORKTREE_ROOTS", str(tmp_path))
    monkeypatch.setenv("RALF_PROGRAMMER_STATE_ROOT", str(tmp_path / "programmer-state"))
    monkeypatch.setenv("RALF_CODE_PROVIDER", "llamacpp-code-local")
    monkeypatch.setenv("RALF_CODE_MODEL", "qwen2.5-coder-7b")
    monkeypatch.setenv("RALF_CODE_FALLBACK_PROVIDER", "agentcpm-local")
    monkeypatch.setenv("RALF_CODE_FALLBACK_MODEL", "AgentCPM-Explore")
    response = TestClient(app).post(
        "/tasks/run",
        json={
            "user_goal": "correggi codice nel repository e verifica il diff",
            "extra_context": {
                "source": "ralf_terminal",
                "terminal_client": {"cwd": str(worktree)},
            },
        },
    )
    payload = response.json()
    assert response.status_code == 200
    assert payload["ok"] is True
    assert payload["capability"] == "local_code_patch"
    assert observed == {
        "workdir": str(worktree.resolve()),
        "validator": "git diff --check",
        "worker_user": "sibilla-cumana",
        "provider": "llamacpp-code-local",
        "model": "qwen2.5-coder-7b",
        "fallback_provider": "agentcpm-local",
        "fallback_model": "AgentCPM-Explore",
    }


def test_patch_allowed_rejects_non_git_terminal_cwd(monkeypatch, tmp_path):
    monkeypatch.setenv("RALF_CODE_WORKTREE_ROOTS", str(tmp_path.parent))
    response = TestClient(app).post(
        "/tasks/run",
        json={
            "user_goal": "correggi codice nel repository",
            "extra_context": {"terminal_client": {"cwd": str(tmp_path)}},
        },
    )
    payload = response.json()
    assert payload["ok"] is False
    assert payload["stop_reason"] == "trusted_git_worktree_required"
