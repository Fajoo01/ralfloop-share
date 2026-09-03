from ralfloop_agent.unified_assistant.memory_service import MemoryService
from ralfloop_agent.unified_assistant.tiremm_admin import TiremmAdminV2
from ralfloop_agent.unified_assistant.tiremm_admin_mcp import TOOLS, TiremmAdminMCPServer
from tiremm_admin_fixture import NOW, build_store


def test_mcp_exposes_only_semantic_v2_capabilities(tmp_path):
    with MemoryService(tmp_path / "memory.sqlite") as memory:
        server = TiremmAdminMCPServer(TiremmAdminV2(memory, build_store()))
        assert {row["name"] for row in server.list_tools()} == set(TOOLS)
        assert not any(word in name for name in TOOLS for word in ("sql", "http", "execute", "delete"))
        result = server.call("tiremm_get_blocked", {})["structuredContent"]
        assert result["ok"] and len(result["result"]) == 2
        assert server.call("generic_api_call", {})["structuredContent"]["status"] == "FORBIDDEN"
        assert server.call("tiremm_get_deadlines", {"now": NOW.isoformat(), "extra": 1})["isError"]


def test_prepare_action_validates_evidence_and_never_executes(tmp_path):
    store = build_store()
    evidence = store.get_sources("practice.famiglia-colloquio")[0]
    proposal = {
        "action": "draft_email", "target": "fixture@example.invalid",
        "payload": {"subject": "Bozza"}, "evidence_ids": [evidence.evidence_id],
        "reason": "Source-backed practice", "requires_approval": True,
    }
    with MemoryService(tmp_path / "memory.sqlite") as memory:
        server = TiremmAdminMCPServer(TiremmAdminV2(memory, store))
        result = server.call("tiremm_prepare_action", {"proposal": proposal})["structuredContent"]
        assert result["ok"] and result["result"]["requires_approval"] is True
        proposal["evidence_ids"] = ["src.000000000000000000000000"]
        assert server.call("tiremm_prepare_action", {"proposal": proposal})["isError"]
