from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from ralfloop_agent.unified_assistant.contracts import AssistantFeatureFlags, PolicyClass
from ralfloop_agent.unified_assistant.conversation import ConversationManager
from ralfloop_agent.unified_assistant.core import UnifiedAssistantCore
from ralfloop_agent.unified_assistant.planner import UnifiedPlanner
from ralfloop_agent.unified_assistant.registry import UnifiedRegistryFacade
from ralfloop_agent.unified_assistant import runtime


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("mailchimp_server", ROOT / "scripts/ralf_mailchimp_mcp_server.py")
assert SPEC and SPEC.loader
SERVER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(SERVER)


class FakeClient:
    def __init__(self):
        self.calls = []

    def list_members(self, list_id, **kwargs):
        self.calls.append(("members", list_id, kwargs))
        return {"results": [{"id": "m1", "email_address": "person@example.invalid", "status": "subscribed", "merge_fields": {"FNAME": "Ada"}, "tags": []}], "total_items": 1, **kwargs}

    def list_segments(self, list_id, **kwargs):
        self.calls.append(("segments", list_id, kwargs))
        return {"results": [{"id": 1, "name": "Soci", "member_count": 3, "type": "static"}], "total_items": 1, **kwargs}

    def list_tags(self, list_id, **kwargs):
        self.calls.append(("tags", list_id, kwargs))
        return {"results": [{"id": 2, "name": "Famiglie", "member_count": 2}]}

    def list_member_tags(self, list_id, subscriber_hash):
        self.calls.append(("member_tags", list_id, subscriber_hash))
        return {"results": [{"id": 2, "name": "Famiglie"}]}


@pytest.mark.parametrize("tool,arguments,operation", [
    ("mailchimp_list_members", {"list_id": "aud_123", "count": 2, "offset": 0, "status": "subscribed"}, "list_members"),
    ("mailchimp_list_segments", {"list_id": "aud_123", "count": 2, "offset": 0}, "list_segments"),
    ("mailchimp_list_tags", {"list_id": "aud_123"}, "list_tags"),
    ("mailchimp_list_member_tags", {"list_id": "aud_123", "subscriber_hash": "a" * 32}, "list_member_tags"),
])
def test_server_semantic_reads_are_zero_effect(tool, arguments, operation):
    result = SERVER.MailchimpMCPServer(FakeClient()).call(tool, arguments)
    assert result["ok"] is True
    assert result["operation"] == operation
    assert (result["side_effects"], result["writes"], result["sends"]) == (0, 0, 0)


@pytest.mark.parametrize("arguments", [
    {"list_id": "x", "count": 0},
    {"list_id": "../lists", "count": 1},
    {"list_id": "valid", "method": "POST"},
    {"list_id": "valid", "url": "https://example.invalid"},
])
def test_server_rejects_invalid_or_injected_member_arguments(arguments):
    result = SERVER.MailchimpMCPServer(FakeClient()).call("mailchimp_list_members", arguments)
    assert result["isError"] is True
    assert result["structuredContent"]["status"] == "POLICY_DENIED"


def test_planner_routes_explicit_mailchimp_reads_only():
    planner = UnifiedPlanner(UnifiedRegistryFacade())
    assert planner.plan("quali segmenti abbiamo su Mailchimp?").assignments[0].arguments["operation"] == "segments"
    assert planner.plan("quali tag ci sono su Mailchimp? list_id=abc123").assignments[0].arguments == {
        "operation": "tags", "count": 10, "offset": 0, "list_id": "abc123"
    }
    assert planner.plan("chi abbiamo su Mailchimp che potrebbe essere interessato a Meet and Code?").assignments[0].arguments["operation"] == "audience_analysis"
    assert planner.plan("quali segmenti abbiamo?").intent != "mailchimp.read"
    assert planner.plan("elenca i contatti").intent != "mailchimp.read"


def test_mailchimp_mutation_remains_denied():
    plan = UnifiedPlanner(UnifiedRegistryFacade()).plan("Mailchimp: aggiungi un contatto")
    assert plan.intent == "assistant.reject"
    assert plan.assignments[0].policy is PolicyClass.DENY


class Gateway:
    def __init__(self):
        self.calls = []

    def invoke_read(self, tool, **arguments):
        self.calls.append((tool, arguments))
        rows = {
            "mailchimp_list_audiences": [{"id": "aud123", "name": "Tiremm"}],
            "mailchimp_list_segments": [{"id": 1, "name": "Soci"}],
            "mailchimp_list_tags": [{"id": 2, "name": "Famiglie"}],
            "mailchimp_list_members": [{"id": "m1", "status": "subscribed"}],
        }[tool]
        return {"ok": True, "results": rows, "side_effects": 0, "writes": 0, "sends": 0}


class Context:
    def __init__(self, gateway): self.gateway = gateway
    def __enter__(self): return self.gateway
    def __exit__(self, *_): return None


def test_complex_analysis_composes_reads_without_confirmation_or_effects():
    gateway = Gateway()
    core = UnifiedAssistantCore(
        planner=UnifiedPlanner(UnifiedRegistryFacade()), conversation=ConversationManager(),
        flags=AssistantFeatureFlags(unified_assistant=True),
        mailchimp_gateway_factory=lambda: Context(gateway),
    )
    result = core.handle("chi abbiamo su Mailchimp che potrebbe essere interessato a Meet and Code?")
    assert result.status == "completed"
    assert [call[0] for call in gateway.calls] == [
        "mailchimp_list_audiences", "mailchimp_list_segments", "mailchimp_list_tags", "mailchimp_list_members"
    ]
    assert result.data["mailchimp"]["results"] == [{"id": "m1", "status": "subscribed"}]
    assert (result.data["side_effects"], result.data["writes"], result.data["sends"]) == (0, 0, 0)
    assert core.conversation.state.pending.model_dump(exclude_none=True) == {}


def test_runtime_artifact_preserves_zero_effects(monkeypatch, tmp_path):
    gateway = Gateway()
    monkeypatch.setenv("RALFLOOP_UNIFIED_ASSISTANT", "1")
    monkeypatch.setenv("RALFLOOP_UNIFIED_SESSION_DIR", str(tmp_path / "sessions"))
    monkeypatch.setattr(runtime.MailchimpMCPContext, "from_environment", lambda: Context(gateway))
    request_context = {
        "source": "telegram_natural", "telegram_user_id": 1,
        "telegram_chat_id": 1, "telegram_message_id": 1,
    }
    result = runtime.run_unified_telegram(
        "chi abbiamo su Mailchimp che potrebbe essere interessato a Meet and Code?",
        request_context,
    )
    assert result["ok"] is True
    assert result["approval_required"] is False
    artifact = result["artifacts"][0]
    assert artifact["operation"] == "audience_analysis"
    assert (artifact["side_effects"], artifact["writes"], artifact["sends"]) == (0, 0, 0)
