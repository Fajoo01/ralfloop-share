from __future__ import annotations

from ralfloop_agent.unified_assistant.github_mcp_adapter import github_read_adapter
from ralfloop_agent.unified_assistant.planner import UnifiedPlanner
from ralfloop_agent.unified_assistant.registry import UnifiedRegistryFacade


class FakeGateway:
    def invoke(self, name, **arguments):
        assert name == "github_issue_get"
        assert arguments == {"repo": "Fajoo01/ralfloop-bottazzi", "number": 51}
        return {
            "ok": True,
            "operation": name,
            "data": {
                "number": 51,
                "state": "open",
                "title": "Scholarly MCP",
                "body": "Technical diary",
                "html_url": "https://github.com/Fajoo01/ralfloop-bottazzi/issues/51",
            },
            "writes": 0,
            "sends": 0,
        }


class FakeContext:
    def __enter__(self):
        return FakeGateway()

    def __exit__(self, *_args):
        return None


def test_github_issue_read_routes_to_unified_skill():
    planner = UnifiedPlanner(UnifiedRegistryFacade())
    plan = planner.validate(
        planner.plan("Leggi GitHub issue #51 di Fajoo01/ralfloop-bottazzi")
    )
    assignment = plan.assignments[0]
    assert plan.intent == "github.read"
    assert assignment.skill == "github.read"
    assert assignment.arguments == {
        "operation": "issue",
        "repo": "Fajoo01/ralfloop-bottazzi",
        "number": 51,
    }


def test_github_read_adapter_is_zero_write():
    planner = UnifiedPlanner(UnifiedRegistryFacade())
    assignment = planner.validate(
        planner.plan("Leggi GitHub issue #51 di Fajoo01/ralfloop-bottazzi")
    ).assignments[0]
    artifact = github_read_adapter(
        assignment,
        {"user.goal": assignment.objective},
        gateway_factory=lambda: FakeContext(),
    )
    assert artifact.status == "completed"
    assert artifact.payload["writes"] == 0
    assert artifact.payload["sends"] == 0
    assert "Scholarly MCP" in artifact.payload["message"]
    assert artifact.evidence_refs == (
        "https://github.com/Fajoo01/ralfloop-bottazzi/issues/51",
    )


def test_github_mutation_fails_closed_without_approval_workflow():
    planner = UnifiedPlanner(UnifiedRegistryFacade())
    plan = planner.plan("Commenta GitHub issue #51 di Fajoo01/ralfloop-bottazzi")
    assert plan.intent == "assistant.reject"
    assert plan.requires_clarification is True
    assert plan.clarification_reason == "github_mutation_requires_approval_workflow"
