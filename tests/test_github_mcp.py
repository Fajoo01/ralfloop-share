from __future__ import annotations

import json
import os
from unittest.mock import patch

from scripts.ralf_github_mcp_server import GitHubMCPServer
from src.github_mcp import GitHubGateway
from ralfloop_agent.core.capability_router import route_task


def test_github_read_route_needs_no_confirmation():
    route = route_task("Leggi GitHub issue #51")
    assert "github" in route.mcp_connectors
    assert route.needs_human_confirmation is False
    assert route.task_mode == "check_only"


def test_github_write_route_requires_confirmation():
    route = route_task("Commenta issue GitHub #51")
    assert "github" in route.mcp_connectors
    assert route.task_mode == "external_action"
    assert route.needs_human_confirmation is True


def test_github_read_server_uses_allowlisted_repo(monkeypatch):
    monkeypatch.setenv("RALF_GITHUB_ALLOWED_REPOS", "Fajoo01/")
    server = GitHubMCPServer()
    with patch("scripts.ralf_github_mcp_server.subprocess.run") as run:
        run.return_value.returncode = 0
        run.return_value.stdout = json.dumps({"full_name": "Fajoo01/ralfloop-bottazzi"})
        run.return_value.stderr = ""
        result = server.call("github_repo_get", {"repo": "Fajoo01/ralfloop-bottazzi"})
    assert result["structuredContent"]["ok"] is True
    assert result["structuredContent"]["writes"] == 0
    assert run.call_args.args[0][:2] == ["gh", "api"]


def test_github_write_is_disabled_on_read_broker(monkeypatch):
    monkeypatch.delenv("RALF_GITHUB_MCP_WRITE_ENABLED", raising=False)
    server = GitHubMCPServer()
    result = server.call(
        "github_issue_comment",
        {"repo": "Fajoo01/ralfloop-bottazzi", "number": 51, "body": "test"},
    )
    payload = result["structuredContent"]
    assert payload["ok"] is False
    assert payload["status"] == "APPROVAL_REQUIRED"
    assert payload["writes"] == 0


def test_github_repo_allowlist_denies_other_owners(monkeypatch):
    monkeypatch.setenv("RALF_GITHUB_ALLOWED_REPOS", "Fajoo01/")
    server = GitHubMCPServer()
    result = server.call("github_repo_get", {"repo": "someone/else"})
    assert result["structuredContent"]["status"] == "POLICY_DENIED"


def test_github_gateway_decodes_transport_mapping_result():
    class Tool:
        def __init__(self, name):
            self.name = name

    class Session:
        def list_tools(self):
            return [Tool(name) for name in sorted({
                "github_repo_get", "github_issue_list", "github_issue_get",
                "github_pr_list", "github_pr_get", "github_issue_create",
                "github_issue_comment",
            })]

        def call_tool(self, name, arguments):
            return {
                "structuredContent": {
                    "ok": True,
                    "operation": name,
                    "data": {"full_name": arguments.get("repo")},
                    "writes": 0,
                }
            }

    gateway = GitHubGateway(Session())
    gateway.discover()
    result = gateway.invoke("github_repo_get", repo="Fajoo01/ralfloop-bottazzi")
    assert result["ok"] is True
    assert result["data"]["full_name"] == "Fajoo01/ralfloop-bottazzi"
