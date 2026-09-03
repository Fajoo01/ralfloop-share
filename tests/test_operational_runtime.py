from datetime import datetime, timezone

from ralfloop_agent.unified_assistant.media_quality import MediaTicketType
from ralfloop_agent.unified_assistant.nightly_worker import NightlyTaskType
from ralfloop_agent.unified_assistant.operational_runtime import BottazziOperationalRuntime
from tests.test_bandi_service import bando, source


NOW = datetime(2026, 9, 3, tzinfo=timezone.utc)


class Adapter:
    source_id = "synthetic_api"
    source_priority = 1
    def fetch(self): return (bando(),)


def test_operational_runtime_routes_bandi_and_media_into_shared_persistent_nightly_queue(tmp_path):
    path = tmp_path / "bottazzi.sqlite"
    with BottazziOperationalRuntime(path) as runtime:
        runtime.poll_bandi((Adapter(),))
        runtime.media.create(
            ticket_type=MediaTicketType.VIDEO_LOW_QUALITY,
            jellyfin_item_id="item-1", user_report="Low resolution",
            source=source("item-1"), reported_at=NOW,
        )
        jobs = runtime.nightly_queue.queued()
        assert {row.task_type for row in jobs} == {
            NightlyTaskType.BANDO_UNCLASSIFIED,
            NightlyTaskType.MEDIA_QUALITY_TICKET,
        }
        metrics = runtime.metrics.snapshot()
        assert metrics["event_received"] == 2
        assert metrics["event_workflow_run"] == 2
        assert metrics["bandi_sources_polled"] == 1
        assert metrics["bandi_new"] == 1
        assert metrics["media_tickets_open"] == 1
    with BottazziOperationalRuntime(path) as runtime:
        assert len(runtime.nightly_queue.queued()) == 2


def test_operational_runtime_runs_deterministic_nightly_without_model(tmp_path):
    handlers = {NightlyTaskType.BANDO_UNCLASSIFIED: lambda job: {"review": job.payload["event_id"]}}
    with BottazziOperationalRuntime(tmp_path / "bottazzi.sqlite", deterministic_handlers=handlers) as runtime:
        runtime.poll_bandi((Adapter(),))
        result = runtime.run_nightly_once(now=datetime(2026, 9, 4, 1, tzinfo=timezone.utc))
        assert result and result.result["review"].startswith("event.bando-")
        assert runtime.metrics.snapshot().get("nightly_llm_calls", 0) == 0
