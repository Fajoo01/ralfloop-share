from datetime import datetime, timezone

from ralfloop_agent.unified_assistant.event_router import EventRouter
from ralfloop_agent.unified_assistant.memory_service import MemoryService
from ralfloop_agent.unified_assistant.nightly_worker import NightlyEventSink, NightlyQueue, NightlyTaskType
from ralfloop_agent.unified_assistant.pec_runts import (
    AuthorityStatus, PecMessage, PecRuntsService, RuntsAuthRequired,
    RuntsMessage, RuntsPractice,
)
from ralfloop_agent.unified_assistant.platform import SourceRef
from ralfloop_agent.unified_assistant.pec_runts_mcp import PecRuntsMCPServer, TOOLS, capability_descriptors
from ralfloop_agent.unified_assistant.platform import CapabilityRegistry
from ralfloop_agent.unified_assistant.operational_runtime import BottazziOperationalRuntime
from ralfloop_agent.unified_assistant.runtsuite_adapter import RuntsuitePracticeLink


NOW = datetime(2026, 9, 3, tzinfo=timezone.utc)


def ref(system, native_id):
    return SourceRef(system=system, native_id=native_id, locator=f"https://{system}.example.invalid/{native_id}", observed_at=NOW.isoformat(), content_hash="1" * 64)


def pec(reference="runts-msg-1"):
    return PecMessage.build(native_id="pec-1", subject="Synthetic RUNTS notification", sender="synthetic@example.invalid", received_at=NOW, observed_at=NOW, body="Consult the authoritative RUNTS communication.", unread=True, certified=True, runts_reference=reference, source=ref("pec", "pec-1"))


def runts_message():
    return RuntsMessage.build(native_id="runts-msg-1", practice_id="practice-1", subject="Synthetic authoritative communication", body="Official administrative content", published_at=NOW, observed_at=NOW, action_required=True, source=ref("runts", "runts-msg-1"))


class PecProvider:
    def list_messages(self, *, limit): return (pec(),)
    def get_message(self, native_id): return pec()


class RuntsProvider:
    def list_messages(self, *, limit): return (runts_message(),)
    def get_message(self, native_id): return runts_message()
    def list_practices(self, *, limit): return (RuntsPractice(native_id="practice-1", status_raw="IN_REVIEW", title="Synthetic practice", updated_at=NOW, observed_at=NOW, action_required=True, source=ref("runts", "practice-1"), content_hash="2" * 64),)
    def get_practice(self, native_id): return self.list_practices(limit=1)[0]


class RuntsuiteProvider:
    def find_runts_practice(self, runts_practice_id):
        return RuntsuitePracticeLink(status="FOUND", runts_practice_id=runts_practice_id, review_ids=("review-1",), content_hashes=("3" * 64,))


def service(memory, runts=None):
    queue = NightlyQueue(memory)
    sink = NightlyEventSink(queue)
    router = EventRouter(memory, enabled_workflows=("runts.correlate", "runts.review"), workflow_handlers={"runts.correlate": sink, "runts.review": sink}, wake_handler=sink)
    return PecRuntsService(memory, PecProvider(), runts or RuntsProvider(), router=router), queue


def test_pec_notification_is_persisted_and_authority_is_runts(tmp_path):
    with MemoryService(tmp_path / "memory.sqlite") as memory:
        vertical, queue = service(memory)
        rows = vertical.find_runts_notifications()
        assert rows[0].runts_reference == "runts-msg-1"
        result = vertical.authoritative_for_pec("pec-1")
        assert result.status is AuthorityStatus.VERIFIED_RUNTS
        assert result.runts_message.body == "Official administrative content"
        assert result.sources[0].system == "pec" and result.sources[1].system == "runts"
        assert memory.search_documents("Official administrative content")
        assert queue.queued()[0].task_type is NightlyTaskType.ADMIN_PRACTICE_UNRESOLVED


def test_runts_sync_emits_practice_and_action_events_without_write(tmp_path):
    with MemoryService(tmp_path / "memory.sqlite") as memory:
        vertical, queue = service(memory)
        rows = vertical.sync_runts()
        assert len(rows) == 2
        types = {row.type for row in memory.timeline("runts.practice." + __import__("hashlib").sha256(b"practice-1").hexdigest()[:24])}
        assert {"RUNTS_PRACTICE_UPDATED", "RUNTS_ACTION_REQUIRED"} <= types
        assert {row.task_type for row in queue.queued()} >= {NightlyTaskType.ADMIN_PRACTICE_UNRESOLVED}


def test_auth_boundary_is_explicit_and_pec_is_not_treated_as_authority(tmp_path):
    class AuthRunts(RuntsProvider):
        def get_message(self, native_id): raise RuntsAuthRequired("spid_required")

    with MemoryService(tmp_path / "memory.sqlite") as memory:
        vertical, _ = service(memory, AuthRunts())
        result = vertical.authoritative_for_pec("pec-1")
        assert result.status is AuthorityStatus.AUTH_REQUIRED
        assert result.runts_message is None
        events = memory.timeline("pec." + __import__("hashlib").sha256(b"pec-1").hexdigest()[:32])
        assert any(row.type == "RUNTS_AUTH_REQUIRED" for row in events)


def test_prepare_is_persistent_non_executable_and_approval_required(tmp_path):
    with MemoryService(tmp_path / "memory.sqlite") as memory:
        vertical, _ = service(memory)
        proposal = vertical.prepare_action("practice-1", "UPLOAD_DOCUMENT", "Document requested by authoritative RUNTS message", (ref("runts", "runts-msg-1"),), now=NOW)
        assert proposal.requires_approval and not proposal.executable
        assert memory.get_entity(proposal.proposal_id).status == "WAITING_APPROVAL"


def test_semantic_mcp_is_strict_and_write_execution_absent(tmp_path):
    with MemoryService(tmp_path / "memory.sqlite") as memory:
        vertical, _ = service(memory)
        server = PecRuntsMCPServer(vertical)
        assert {row["name"] for row in server.list_tools()} == set(TOOLS)
        result = server.call("runts_get_authoritative_for_pec", {"pec_message_id": "pec-1"})
        assert result["structuredContent"]["authority"]["status"] == "VERIFIED_RUNTS"
        assert result["structuredContent"]["writes"] == 0
        assert server.call("runts_execute", {"anything": True})["isError"]


def test_capability_retrieval_isolated_and_bottazzi_invokes_real_vertical(tmp_path):
    registry = CapabilityRegistry(capability_descriptors())
    selected = registry.retrieve("trova notifiche PEC collegate al RUNTS", limit=3)
    assert selected[0].capability_id == "pec_find_runts_notifications"
    assert all(row.domain == "pec_runts" for row in selected)
    with BottazziOperationalRuntime(tmp_path / "runtime.sqlite", pec_provider=PecProvider(), runts_provider=RuntsProvider()) as runtime:
        result = runtime.invoke_pec_runts("trova notifiche PEC collegate al RUNTS", {})
        assert result["selectedCapability"] == "pec_find_runts_notifications"
        assert result["structuredContent"]["messages"][0]["native_id"] == "pec-1"
        assert runtime.nightly_queue.queued()


def test_authoritative_runts_practice_correlates_exactly_to_runtsuite(tmp_path):
    with MemoryService(tmp_path / "memory.sqlite") as memory:
        vertical = PecRuntsService(memory, PecProvider(), RuntsProvider(), runtsuite=RuntsuiteProvider())
        link = vertical.correlate_runtsuite("practice-1")
        assert link.status == "FOUND" and link.review_ids == ("review-1",)
