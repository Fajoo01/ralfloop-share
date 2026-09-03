from datetime import datetime, timezone

from ralfloop_agent.unified_assistant.memory_service import MemoryService
from ralfloop_agent.unified_assistant.nightly_worker import (
    ModelRunResult, ModelTier, NightlyJobState, NightlyQueue,
    NightlyEventSink, NightlyTaskType, NightlyWorker,
)
from tests.test_event_router import event
from ralfloop_agent.unified_assistant.event_router import EventDecision, EventRouter
from ralfloop_agent.unified_assistant.platform import SourceRef


NIGHT = datetime(2026, 9, 4, 1, tzinfo=timezone.utc)
DAY = datetime(2026, 9, 3, 12, tzinfo=timezone.utc)


def source():
    return SourceRef(system="bandi", native_id="bando-1", locator="fixture:1", observed_at=NIGHT.isoformat())


class Provider:
    def __init__(self, tier, *, confidence=1, resolved=True):
        self.tier, self.provider_id = tier, f"fixture-{tier}"
        self.confidence, self.resolved, self.calls = confidence, resolved, []

    def run(self, job):
        self.calls.append(job.job_id)
        return ModelRunResult(output={"provider": self.provider_id}, input_tokens=10, output_tokens=5, duration_ms=2, confidence=self.confidence, resolved=self.resolved)


def test_queue_is_shared_memory_persistent_idempotent_and_rejects_secrets(tmp_path):
    path = tmp_path / "memory.sqlite"
    with MemoryService(path) as memory:
        queue = NightlyQueue(memory)
        first = queue.enqueue(NightlyTaskType.BANDO_CHANGED, {"bando_id": "bando-1"}, source(), now=NIGHT)
        assert queue.enqueue(NightlyTaskType.BANDO_CHANGED, {"bando_id": "bando-1"}, source(), now=NIGHT) == first
        try:
            queue.enqueue(NightlyTaskType.RESEARCH, {"nested": [{"token": "forbidden"}]}, source(), now=NIGHT)
        except ValueError as exc:
            assert str(exc) == "nightly_payload_secret_forbidden"
        else:
            raise AssertionError("secret accepted")
    with MemoryService(path) as memory:
        assert NightlyQueue(memory).get(first.job_id) == first


def test_day_does_not_run_and_deterministic_handler_avoids_llm(tmp_path):
    with MemoryService(tmp_path / "memory.sqlite") as memory:
        queue = NightlyQueue(memory)
        job = queue.enqueue(NightlyTaskType.PRACTICE_STALE, {"practice_id": "p1"}, source(), complexity=8, now=DAY)
        qwen = Provider(ModelTier.QWEN35)
        worker = NightlyWorker(queue, {ModelTier.QWEN35: qwen}, deterministic_handlers={NightlyTaskType.PRACTICE_STALE: lambda _: {"action": "review"}})
        assert worker.run_once(now=DAY) is None
        result = worker.run_once(now=NIGHT)
        assert result and result.state is NightlyJobState.COMPLETED
        assert result.result == {"action": "review"}
        assert qwen.calls == []
        assert memory.timeline(job.job_id)


def test_model_routing_simple_qwen_largest_and_escalation_metrics(tmp_path):
    with MemoryService(tmp_path / "memory.sqlite") as memory:
        queue = NightlyQueue(memory)
        simple_job = queue.enqueue(NightlyTaskType.RESEARCH, {"case": "simple"}, source(), complexity=2, priority=10, now=NIGHT)
        qwen_job = queue.enqueue(NightlyTaskType.COMPLEX_DOCUMENT, {"case": "medium"}, source(), complexity=6, priority=9, now=NIGHT)
        large_job = queue.enqueue(NightlyTaskType.SOURCE_INCONSISTENCY, {"case": "hard"}, source(), complexity=10, priority=8, now=NIGHT)
        simple = Provider(ModelTier.SIMPLE_LOCAL, confidence=.2)
        qwen = Provider(ModelTier.QWEN35)
        largest = Provider(ModelTier.LARGEST)
        worker = NightlyWorker(queue, {ModelTier.SIMPLE_LOCAL: simple, ModelTier.QWEN35: qwen, ModelTier.LARGEST: largest})

        first = worker.run_once(now=NIGHT)
        second = worker.run_once(now=NIGHT)
        third = worker.run_once(now=NIGHT)

        assert first.job_id == simple_job.job_id and first.model_tier is ModelTier.QWEN35
        assert first.model_id == qwen.provider_id
        assert second.job_id == qwen_job.job_id and second.model_tier is ModelTier.QWEN35
        assert third.job_id == large_job.job_id and third.model_tier is ModelTier.LARGEST
        metrics = worker.metrics.snapshot()
        assert metrics["nightly_jobs"] == 3
        assert metrics["nightly_llm_calls"] == 4
        assert metrics["nightly_escalations"] == 1
        timeline = memory.timeline(simple_job.job_id)
        assert timeline[-1].payload["escalation_reason"] == "unresolved_or_low_confidence"
        assert timeline[-1].payload["model"] == qwen.provider_id


def test_missing_provider_fails_closed_without_retry_loop(tmp_path):
    with MemoryService(tmp_path / "memory.sqlite") as memory:
        queue = NightlyQueue(memory)
        queue.enqueue(NightlyTaskType.RESEARCH, {"case": "medium"}, source(), complexity=6, now=NIGHT)
        result = NightlyWorker(queue, {}).run_once(now=NIGHT)
        assert result and result.state is NightlyJobState.FAILED
        assert result.error == "RuntimeError"
        assert NightlyWorker(queue, {}).run_once(now=NIGHT) is None


def test_event_router_enqueues_nightly_job_without_waking_model_now(tmp_path):
    with MemoryService(tmp_path / "memory.sqlite") as memory:
        queue = NightlyQueue(memory)
        sink = NightlyEventSink(queue)
        router = EventRouter(
            memory, enabled_workflows=("bandi.review",),
            workflow_handlers={"bandi.review": sink}, wake_handler=sink,
        )
        routed = router.route(event("BANDO_UPDATED"))
        assert routed.decision is EventDecision.RUN_WORKFLOW
        jobs = queue.queued()
        assert len(jobs) == 1
        assert jobs[0].task_type is NightlyTaskType.BANDO_CHANGED
        assert jobs[0].model_tier is None
