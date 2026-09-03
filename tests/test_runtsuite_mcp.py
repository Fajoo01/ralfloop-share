from ralfloop_agent.unified_assistant.runtsuite_adapter import RuntsuiteMember, RuntsuitePracticeLink
from ralfloop_agent.unified_assistant.runtsuite_mcp import RuntsuiteMCPServer, TOOLS


class Fixture:
    def get_member(self, external_member_id):
        return RuntsuiteMember(status="FOUND", runtsuite_identity_id="7", external_member_id=external_member_id, active=True)
    def list_projects(self): return [{"project_id": 1}]
    def list_funding_calls(self): return []
    def list_meetings(self): return []
    def list_attendance(self): return []
    def list_member_cards(self): return []
    def list_member_account_links(self): return []
    def list_review_queue(self): return []
    def find_runts_practice(self, runts_practice_id): return RuntsuitePracticeLink(status="FOUND", runts_practice_id=runts_practice_id, review_ids=("review-7",), content_hashes=("1" * 64,))


def test_runtsuite_mcp_exposes_only_observed_explicit_read_capabilities():
    server = RuntsuiteMCPServer(Fixture())
    assert {row["name"] for row in server.list_tools()} == set(TOOLS)
    assert all(row["inputSchema"]["additionalProperties"] is False for row in server.list_tools())
    assert not any("execute" in name or "delete" in name or "update" in name for name in TOOLS)


def test_runtsuite_member_lookup_is_exact_native_mapping():
    result = RuntsuiteMCPServer(Fixture()).call(
        "runtsuite_get_member", {"external_member_id": "arci-stable-1"}
    )
    assert result["structuredContent"]["result"] == {
        "status": "FOUND", "runtsuite_identity_id": "7",
        "external_member_id": "arci-stable-1", "active": True,
    }


def test_runtsuite_write_and_generic_calls_are_denied():
    server = RuntsuiteMCPServer(Fixture())
    assert server.call("runtsuite_execute", {"anything": "x"})["isError"]
    assert server.call("runtsuite_list_projects", {"unexpected": True})["isError"]


def test_runtsuite_exact_runts_practice_link_is_typed_and_pii_minimized():
    result = RuntsuiteMCPServer(Fixture()).call("runtsuite_find_runts_practice", {"runts_practice_id": "runts-practice-1"})
    assert result["structuredContent"]["result"] == {"status": "FOUND", "runts_practice_id": "runts-practice-1", "review_ids": ["review-7"], "content_hashes": ["1" * 64]}
