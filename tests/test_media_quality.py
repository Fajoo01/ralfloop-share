from datetime import datetime, timezone

from ralfloop_agent.unified_assistant.event_router import EventRouter
from ralfloop_agent.unified_assistant.jellyfin_semantic import JellyfinMediaStream
from ralfloop_agent.unified_assistant.media_quality import (
    MediaEvidence, MediaQualityService, MediaTicketState, MediaTicketType,
)
from ralfloop_agent.unified_assistant.media_quality_mcp import MediaQualityMCPServer, TOOLS, media_capability_descriptors
from ralfloop_agent.unified_assistant.memory_service import MemoryService
from ralfloop_agent.unified_assistant.nightly_worker import NightlyEventSink, NightlyQueue, NightlyTaskType
from ralfloop_agent.unified_assistant.platform import SourceRef
from ralfloop_agent.unified_assistant.platform import CapabilityRegistry
from ralfloop_agent.unified_assistant.service_identity_mcp import JellyfinReadOnlyClient


NOW = datetime(2026, 9, 3, tzinfo=timezone.utc)


def source(native_id="item-1"):
    return SourceRef(system="jellyfin", native_id=native_id, locator="fixture:playback", observed_at=NOW.isoformat())


class Reader:
    def __init__(self, streams=(), error=False):
        self.streams, self.error = tuple(streams), error

    def get_media_streams(self, item_id, *, user_id):
        if self.error:
            raise RuntimeError("offline")
        return self.streams


class Catalog:
    def __init__(self, ids, total):
        self.ids, self.total = tuple(ids), total

    def list_library_item_ids(self, library_id, *, user_id, limit):
        return {"ids": self.ids[:limit], "total": self.total}

    def get_playback_context(self, item_id, *, user_id):
        return {"item_id": item_id, "play_method": "DirectPlay"}


def streams(*, audio="eng", subtitle=None, height=480, audio_bitrate=64_000, audio_title=None):
    rows = [JellyfinMediaStream(media_source_id="source-1", index=0, type="Video", codec="h264", width=720, height=height, bitrate=800_000)]
    rows.append(JellyfinMediaStream(media_source_id="source-1", index=1, type="Audio", codec="aac", language=audio, display_title=audio_title, bitrate=audio_bitrate))
    if subtitle:
        rows.append(JellyfinMediaStream(media_source_id="source-1", index=2, type="Subtitle", codec="srt", language=subtitle))
    return rows


def test_ticket_persists_deduplicates_redacts_pii_and_queues_nightly(tmp_path):
    path = tmp_path / "memory.sqlite"
    with MemoryService(path) as memory:
        queue = NightlyQueue(memory)
        sink = NightlyEventSink(queue)
        router = EventRouter(memory, enabled_workflows=("media.triage",), workflow_handlers={"media.triage": sink})
        service = MediaQualityService(memory, router=router)
        first = service.create(ticket_type=MediaTicketType.AUDIO_MISSING_ITALIAN, jellyfin_item_id="item-1", user_report="write me at synthetic@example.invalid or +39 000 000 0000", source=source(), reported_at=NOW)
        second = service.create(ticket_type=MediaTicketType.AUDIO_MISSING_ITALIAN, jellyfin_item_id="item-1", user_report="same issue", source=source(), reported_at=NOW)
        assert first == second
        assert "synthetic@example.invalid" not in first.user_report and "000" not in first.user_report
        try:
            service.scan_item("item-2", user_id="user-1", reader=Reader(error=True))
        except RuntimeError:
            pass
        jobs = queue.queued()
        assert len(jobs) == 2
        assert all(row.task_type is NightlyTaskType.MEDIA_QUALITY_TICKET for row in jobs)
    with MemoryService(path) as memory:
        assert MediaQualityService(memory).get(first.ticket_id) == first


def test_deterministic_scan_covers_realistic_findings_and_source_failure(tmp_path):
    with MemoryService(tmp_path / "memory.sqlite") as memory:
        service = MediaQualityService(memory)
        result = service.scan_item("item-1", user_id="user-1", reader=Reader(streams()))
        assert set(result.findings) == {
            MediaTicketType.VIDEO_LOW_QUALITY, MediaTicketType.AUDIO_LOW_QUALITY,
            MediaTicketType.AUDIO_MISSING_ITALIAN, MediaTicketType.SUBTITLE_MISSING,
        }
        metadata_wrong = service.scan_item("item-2", user_id="user-1", reader=Reader(streams(audio="und", audio_title="AAC Italiano", subtitle="ita", height=1080, audio_bitrate=128_000)))
        assert metadata_wrong.findings == (MediaTicketType.AUDIO_WRONG_LANGUAGE,)
        client_only = service.scan_item("item-4", user_id="user-1", reader=Reader(streams(audio="ita", subtitle="ita", height=1080, audio_bitrate=128_000)))
        assert client_only.findings == ()
        try:
            service.scan_item("item-3", user_id="user-1", reader=Reader(error=True))
        except RuntimeError as exc:
            assert str(exc) == "media_source_unavailable"
        else:
            raise AssertionError("source outage treated as success")


def test_diagnosis_proposal_and_verification_are_memory_backed_non_executable(tmp_path):
    with MemoryService(tmp_path / "memory.sqlite") as memory:
        service = MediaQualityService(memory)
        ticket = service.create(ticket_type=MediaTicketType.WRONG_METADATA, jellyfin_item_id="item-1", user_report="Italian audio is tagged unknown", source=source(), reported_at=NOW)
        diagnosed = service.diagnose(ticket.ticket_id, (MediaTicketType.AUDIO_WRONG_LANGUAGE,), source(), now=NOW)
        proposal = service.prepare_fix(diagnosed.ticket_id, action="CORRECT_LANGUAGE_METADATA", reason="Existing Italian track has wrong metadata", reversible=True, source=source(), now=NOW)
        assert proposal.requires_approval and not proposal.executable
        assert service.get(ticket.ticket_id).state is MediaTicketState.WAITING_APPROVAL
        evidence = MediaEvidence(kind="rescan", summary="Language now ita", observed_at=NOW, source=source())
        resolved = service.verify_fix(ticket.ticket_id, passed=True, evidence=evidence)
        assert resolved.state is MediaTicketState.RESOLVED
        assert memory.get_entity(proposal.proposal_id).status == "WAITING_APPROVAL"


def test_mcp_exposes_semantic_surface_and_fail_closed_catalog(tmp_path):
    with MemoryService(tmp_path / "memory.sqlite") as memory:
        server = MediaQualityMCPServer(MediaQualityService(memory), Reader(streams(audio="ita", subtitle="ita", height=1080, audio_bitrate=128_000)))
        assert {row["name"] for row in server.list_tools()} == set(TOOLS)
        created = server.call("media_ticket_create", {"ticket_type": "OTHER", "jellyfin_item_id": "item-1", "user_report": "problem"})
        assert created["structuredContent"]["writes"] == 0
        scan = server.call("media_scan_item", {"item_id": "item-1", "user_id": "user-1"})
        assert scan["structuredContent"]["scan"]["findings"] == []
        unavailable = server.call("media_scan_library", {"library_id": "lib-1", "user_id": "user-1"})
        assert unavailable["isError"] and unavailable["structuredContent"]["status"] == "SOURCE_UNAVAILABLE"
        denied = server.call("generic_execute", {})
        assert denied["isError"] and denied["structuredContent"]["status"] == "POLICY_DENIED"


def test_capability_retrieval_selects_media_not_other_domains():
    selected = CapabilityRegistry(media_capability_descriptors()).retrieve("questo episodio è solo inglese, segnala e analizza le tracce", limit=4)
    ids = {row.capability_id for row in selected}
    assert {"media_ticket_create", "media_scan_item", "media_get_streams"} <= ids
    assert all(row.domain == "media_quality" for row in selected)


def test_library_scan_requires_total_unique_reconciliation(tmp_path):
    reader = Reader(streams(audio="ita", subtitle="ita", height=1080, audio_bitrate=128_000))
    with MemoryService(tmp_path / "memory.sqlite") as memory:
        incomplete = MediaQualityMCPServer(MediaQualityService(memory), reader, catalog=Catalog(("item-1",), 2))
        result = incomplete.call("media_scan_library", {"library_id": "lib-1", "user_id": "user-1", "limit": 100})
        assert result["isError"] and result["structuredContent"]["status"] == "INCOMPLETE_SOURCE"
        complete = MediaQualityMCPServer(MediaQualityService(memory), reader, catalog=Catalog(("item-1", "item-2"), 2))
        result = complete.call("media_scan_library", {"library_id": "lib-1", "user_id": "user-1", "limit": 100})
        assert result["structuredContent"]["complete"]
        assert result["structuredContent"]["received"] == result["structuredContent"]["expected"] == 2


def test_jellyfin_library_enumeration_paginates_and_reconciles_total():
    class Client(JellyfinReadOnlyClient):
        def __init__(self):
            super().__init__("http://fixture.invalid", "synthetic")
            self.starts = []

        def _get_json(self, path):
            from urllib.parse import parse_qs, urlsplit
            query = parse_qs(urlsplit(path).query)
            start, size = int(query["StartIndex"][0]), int(query["Limit"][0])
            self.starts.append(start)
            stop = min(start + size, 205)
            return {"Items": [{"Id": f"item-{index}"} for index in range(start, stop)], "TotalRecordCount": 205}

    client = Client()
    listing = client.list_library_item_ids("library-1", user_id="user-1", limit=300)
    assert len(listing["ids"]) == listing["total"] == 205
    assert client.starts == [0, 100, 200]
