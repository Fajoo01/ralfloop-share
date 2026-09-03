from ralfloop_agent.unified_assistant.memory_service import MemoryService
from ralfloop_agent.unified_assistant.tiremm_admin import TiremmAdminV2
from tiremm_admin_fixture import NOW, build_store


def test_v2_persists_restores_and_preserves_v1_projection(tmp_path):
    original = build_store()
    with MemoryService(tmp_path / "memory.sqlite") as memory:
        v2 = TiremmAdminV2(memory)
        source_ids = set()
        practices = (*original.list_open_practices(), original.get_practice("practice.evento-quartiere"))
        for practice in practices:
            records = tuple(original.source_record(identity) for identity in practice.evidence_ids if identity not in source_ids)
            v2.ingest_snapshot(records)
            source_ids.update(record.evidence_id for record in records)
            assert v2.project(practice) == practice
        restored = TiremmAdminV2.restore(memory)
        assert restored.store.metrics() == original.metrics()
        assert restored.list_open_practices() == original.list_open_practices()
        assert restored.get_practice("practice.tari-sede") == original.get_practice("practice.tari-sede")
        assert restored.get_sources("practice.tari-sede") == original.get_sources("practice.tari-sede")
        assert restored.get_timeline("practice.tari-sede")[0].type == "PRACTICE_UPDATED"


def test_v2_deterministic_capability_methods_delegate_without_llm(tmp_path):
    original = build_store()
    with MemoryService(tmp_path / "memory.sqlite") as memory:
        v2 = TiremmAdminV2(memory, original)
        assert v2.get_blocked() == original.blocked()
        assert v2.get_waiting() == original.waiting()
        assert v2.get_next_actions() == original.get_next_actions()
        assert v2.get_deadlines(now=NOW) == original.get_due_practices(now=NOW)
