from datetime import datetime, timezone

import pytest

from ralfloop_agent.unified_assistant.memory_service import MemoryDocument, MemoryEntity, MemoryEvent, MemoryService
from ralfloop_agent.unified_assistant.platform import SourceRef


NOW = datetime(2026, 9, 3, tzinfo=timezone.utc)


def source(native_id="m1"):
    return SourceRef(system="gmail", native_id=native_id, locator=f"gmail:{native_id}", observed_at=NOW.isoformat())


def test_event_store_is_append_only_deduplicated_and_queryable(tmp_path):
    event = MemoryEvent.build(event_id="event.mail-1", type="EMAIL_RECEIVED", source="gmail", source_id="m1", occurred_at=NOW, observed_at=NOW, entity_refs=("practice.tari",), payload={"subject": "TARI"}, provenance=(source(),))
    with MemoryService(tmp_path / "memory.sqlite") as service:
        assert service.append_event(event) is True
        assert service.append_event(event) is False
        assert service.timeline("practice.tari") == (event,)
        conflict = event.model_copy(update={"content_hash": "0" * 64})
        with pytest.raises(ValueError, match="event_conflict"):
            service.append_event(conflict)


def test_document_fts_keeps_source_provenance_and_updates_index(tmp_path):
    with MemoryService(tmp_path / "memory.sqlite") as service:
        first = MemoryDocument.build(document_id="document.tari", title="TARI", body="Scadenza pagamento ottobre", source=source("d1"))
        assert service.put_document(first) is True
        assert service.put_document(first) is False
        assert service.search_documents("pagamento")[0].source.native_id == "d1"
        changed = MemoryDocument.build(document_id="document.tari", title="TARI", body="Pagamento rinviato novembre", source=source("d2"))
        assert service.put_document(changed) is True
        assert service.search_documents("ottobre") == ()
        assert service.search_documents("novembre")[0].content_hash == changed.content_hash
        assert isinstance(service.search_documents('novembre OR "'), tuple)


def test_generic_domain_entity_store_is_persistent_and_provenance_required(tmp_path):
    entity = MemoryEntity.build(
        entity_id="ticket.media-1", domain="media_quality", entity_type="MEDIA_TICKET",
        status="OPEN", updated_at=NOW, data={"ticket_id": "ticket.media-1"},
        provenance=(source("scanner-1"),),
    )
    path = tmp_path / "memory.sqlite"
    with MemoryService(path) as service:
        assert service.put_entity(entity) is True
        assert service.put_entity(entity) is False
    with MemoryService(path) as service:
        assert service.get_entity(entity.entity_id) == entity
        assert service.list_entities(domain="media_quality") == (entity,)
