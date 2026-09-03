from ralfloop_agent.unified_assistant.memory_mcp import MemoryMCPServer
from ralfloop_agent.unified_assistant.memory_service import MemoryService


def test_memory_mcp_is_semantic_read_only_and_fail_closed(tmp_path):
    with MemoryService(tmp_path / "memory.sqlite") as service:
        server = MemoryMCPServer(service)
        names = {row["name"] for row in server.list_tools()}
        assert names == {"memory_get_practice", "memory_search_documents", "memory_get_timeline", "memory_get_open_practices"}
        assert not any(word in name for name in names for word in ("sql", "execute", "write", "delete"))
        assert server.call("memory_get_practice", {"practice_id": "practice.missing"})["structuredContent"]["status"] == "NOT_FOUND"
        assert server.call("generic_api_call", {})["structuredContent"]["status"] == "FORBIDDEN"
        assert server.call("memory_get_timeline", {"entity_ref": "x", "extra": True})["isError"] is True
