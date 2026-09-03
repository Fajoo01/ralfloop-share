from __future__ import annotations

import hashlib
import json
from datetime import datetime, time, timezone
from enum import StrEnum
from typing import Any, Callable, Mapping, Protocol
from zoneinfo import ZoneInfo

from pydantic import Field, model_validator

from .contracts import StrictModel
from .memory_service import MemoryEntity, MemoryEvent, MemoryService
from .observability import OperationalMetrics
from .platform import SourceRef


ROME = ZoneInfo("Europe/Rome")


class NightlyTaskType(StrEnum):
    BANDO_UNCLASSIFIED = "BANDO_UNCLASSIFIED"
    BANDO_CHANGED = "BANDO_CHANGED"
    ELIGIBILITY_AMBIGUOUS = "ELIGIBILITY_AMBIGUOUS"
    COMPLEX_DOCUMENT = "COMPLEX_DOCUMENT"
    ADMIN_PRACTICE_UNRESOLVED = "ADMIN_PRACTICE_UNRESOLVED"
    MEDIA_QUALITY_TICKET = "MEDIA_QUALITY_TICKET"
    DUPLICATE_CONFLICT = "DUPLICATE_CONFLICT"
    RESEARCH = "RESEARCH"
    PRACTICE_STALE = "PRACTICE_STALE"
    SOURCE_INCONSISTENCY = "SOURCE_INCONSISTENCY"


class NightlyJobState(StrEnum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    BLOCKED = "BLOCKED"


class ModelTier(StrEnum):
    SIMPLE_LOCAL = "SIMPLE_LOCAL"
    QWEN35 = "QWEN35"
    LARGEST = "LARGEST"


class NightlyJob(StrictModel):
    job_id: str = Field(pattern=r"^nightly\.[a-f0-9]{32}$")
    task_type: NightlyTaskType
    state: NightlyJobState = NightlyJobState.QUEUED
    priority: int = Field(default=0, ge=0, le=100)
    complexity: int = Field(default=5, ge=0, le=10)
    payload: dict[str, Any]
    source: SourceRef
    created_at: datetime
    updated_at: datetime
    attempts: int = Field(default=0, ge=0, le=10)
    result: dict[str, Any] | None = None
    error: str | None = Field(default=None, max_length=500)
    model_tier: ModelTier | None = None
    model_id: str | None = Field(default=None, max_length=100)

    @model_validator(mode="after")
    def terminal_shape(self) -> "NightlyJob":
        if self.state is NightlyJobState.COMPLETED and self.result is None:
            raise ValueError("nightly_completed_result_required")
        return self


class ModelRunResult(StrictModel):
    output: dict[str, Any]
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    duration_ms: float = Field(ge=0)
    tool_calls: int = Field(default=0, ge=0)
    resolved: bool = True
    confidence: float = Field(default=1, ge=0, le=1)


class ModelProvider(Protocol):
    provider_id: str
    tier: ModelTier

    def run(self, job: NightlyJob) -> ModelRunResult: ...


class ModelRoutingPolicy(StrictModel):
    simple_max_complexity: int = Field(default=3, ge=0, le=10)
    qwen_max_complexity: int = Field(default=8, ge=0, le=10)
    escalate_below_confidence: float = Field(default=0.6, ge=0, le=1)

    def tier_for(self, job: NightlyJob) -> ModelTier:
        if job.complexity <= self.simple_max_complexity:
            return ModelTier.SIMPLE_LOCAL
        if job.complexity <= self.qwen_max_complexity:
            return ModelTier.QWEN35
        return ModelTier.LARGEST


class NightlyQueue:
    def __init__(self, memory: MemoryService) -> None:
        self.memory = memory

    def enqueue(
        self, task_type: NightlyTaskType, payload: dict[str, Any], source: SourceRef,
        *, priority: int = 0, complexity: int = 5, now: datetime | None = None,
    ) -> NightlyJob:
        _reject_secrets(payload)
        stamp = now or datetime.now(timezone.utc)
        canonical = json.dumps({"task_type": task_type, "payload": payload, "source": source.native_id}, sort_keys=True, separators=(",", ":"), default=str)
        identity = "nightly." + hashlib.sha256(canonical.encode()).hexdigest()[:32]
        existing = self.get(identity)
        if existing:
            return existing
        job = NightlyJob(job_id=identity, task_type=task_type, payload=payload, source=source, priority=priority, complexity=complexity, created_at=stamp, updated_at=stamp)
        self._put(job)
        return job

    def get(self, job_id: str) -> NightlyJob | None:
        row = self.memory.get_entity(job_id)
        return NightlyJob.model_validate(row.data) if row and row.domain == "nightly" else None

    def queued(self) -> tuple[NightlyJob, ...]:
        rows = self.memory.list_entities(domain="nightly", status=NightlyJobState.QUEUED, limit=100)
        jobs = [NightlyJob.model_validate(row.data) for row in rows]
        return tuple(sorted(jobs, key=lambda row: (-row.priority, row.created_at, row.job_id)))

    def update(self, job: NightlyJob) -> None:
        self._put(job)

    def _put(self, job: NightlyJob) -> None:
        self.memory.put_entity(MemoryEntity.build(
            entity_id=job.job_id, domain="nightly", entity_type="NIGHTLY_JOB",
            status=job.state, updated_at=job.updated_at,
            data=job.model_dump(mode="json"), provenance=(job.source,),
        ))


class NightlyWorker:
    def __init__(
        self, queue: NightlyQueue, providers: Mapping[ModelTier, ModelProvider],
        *, deterministic_handlers: Mapping[NightlyTaskType, Callable[[NightlyJob], dict[str, Any] | None]] | None = None,
        policy: ModelRoutingPolicy | None = None, metrics: OperationalMetrics | None = None,
    ) -> None:
        self.queue = queue
        self.providers = dict(providers)
        self.handlers = dict(deterministic_handlers or {})
        self.policy = policy or ModelRoutingPolicy()
        self.metrics = metrics or OperationalMetrics()

    def run_once(self, *, now: datetime | None = None, force: bool = False) -> NightlyJob | None:
        stamp = now or datetime.now(timezone.utc)
        if not force and not _night_window(stamp):
            return None
        queued = self.queue.queued()
        if not queued:
            return None
        job = queued[0].model_copy(update={"state": NightlyJobState.RUNNING, "attempts": queued[0].attempts + 1, "updated_at": stamp})
        self.queue.update(job)
        self.metrics.increment("nightly_jobs")
        try:
            handler = self.handlers.get(job.task_type)
            deterministic = handler(job) if handler else None
            if deterministic is not None:
                completed = job.model_copy(update={"state": NightlyJobState.COMPLETED, "result": deterministic, "updated_at": stamp})
                self.queue.update(completed)
                self._event(completed, "NIGHTLY_JOB_COMPLETED")
                return completed
            result, tier, model_id, escalation_reason = self._model_run(job)
            completed = job.model_copy(update={"state": NightlyJobState.COMPLETED, "result": result.output, "model_tier": tier, "model_id": model_id, "updated_at": stamp})
            self.queue.update(completed)
            if escalation_reason:
                self.metrics.increment("nightly_escalations")
                if completed.task_type is NightlyTaskType.MEDIA_QUALITY_TICKET:
                    self.metrics.increment("media_escalated")
            self._event(completed, "NIGHTLY_JOB_COMPLETED", usage=result, escalation_reason=escalation_reason)
            return completed
        except Exception as exc:
            self.metrics.increment("nightly_failures")
            failed = job.model_copy(update={"state": NightlyJobState.FAILED, "error": type(exc).__name__, "updated_at": stamp})
            self.queue.update(failed)
            self._event(failed, "NIGHTLY_JOB_FAILED")
            return failed

    def _model_run(self, job: NightlyJob) -> tuple[ModelRunResult, ModelTier, str, str | None]:
        tier = self.policy.tier_for(job)
        provider = self.providers.get(tier)
        if provider is None:
            raise RuntimeError("model_provider_unavailable")
        self.metrics.increment("nightly_llm_calls")
        result = provider.run(job)
        escalation_reason = None
        if (not result.resolved or result.confidence < self.policy.escalate_below_confidence) and tier is not ModelTier.LARGEST:
            larger = ModelTier.QWEN35 if tier is ModelTier.SIMPLE_LOCAL else ModelTier.LARGEST
            provider = self.providers.get(larger)
            if provider is None:
                raise RuntimeError("escalation_provider_unavailable")
            self.metrics.increment("nightly_llm_calls")
            result, tier = provider.run(job), larger
            escalation_reason = "unresolved_or_low_confidence"
        return result, tier, provider.provider_id, escalation_reason

    def _event(self, job: NightlyJob, event_type: str, usage: ModelRunResult | None = None, escalation_reason: str | None = None) -> None:
        payload = {"job_id": job.job_id, "state": job.state, "model": job.model_id, "model_tier": job.model_tier, "outcome": bool(job.result)}
        if usage:
            payload["usage"] = {"input_tokens": usage.input_tokens, "output_tokens": usage.output_tokens, "duration_ms": usage.duration_ms, "tool_calls": usage.tool_calls}
        if escalation_reason:
            payload["escalation_reason"] = escalation_reason
        event = MemoryEvent.build(
            event_id="event." + hashlib.sha256(f"{job.job_id}|{job.state}|{job.attempts}".encode()).hexdigest()[:32],
            type=event_type, source="nightly_worker", source_id=job.job_id,
            occurred_at=job.updated_at, observed_at=job.updated_at,
            entity_refs=(job.job_id,), payload=payload, provenance=(job.source,),
        )
        self.queue.memory.append_event(event)


class NightlyEventSink:
    """Event Router handler: deterministic event-to-nightly-task mapping."""

    TASKS = {
        "BANDO_DISCOVERED": (NightlyTaskType.BANDO_UNCLASSIFIED, 5),
        "BANDO_UPDATED": (NightlyTaskType.BANDO_CHANGED, 6),
        "BANDO_DEADLINE_CHANGED": (NightlyTaskType.BANDO_CHANGED, 4),
        "BANDO_DOCUMENT_CHANGED": (NightlyTaskType.COMPLEX_DOCUMENT, 7),
        "BANDO_FAQ_CHANGED": (NightlyTaskType.COMPLEX_DOCUMENT, 7),
        "MEDIA_TICKET_CREATED": (NightlyTaskType.MEDIA_QUALITY_TICKET, 4),
        "MEDIA_LOW_QUALITY_DETECTED": (NightlyTaskType.MEDIA_QUALITY_TICKET, 3),
        "MEDIA_LANGUAGE_MISSING": (NightlyTaskType.MEDIA_QUALITY_TICKET, 3),
        "MEDIA_PROBE_FAILED": (NightlyTaskType.MEDIA_QUALITY_TICKET, 7),
        "PRACTICE_STALE": (NightlyTaskType.PRACTICE_STALE, 4),
        "SOURCE_INCONSISTENCY": (NightlyTaskType.SOURCE_INCONSISTENCY, 9),
        "PEC_RUNTS_NOTIFICATION": (NightlyTaskType.ADMIN_PRACTICE_UNRESOLVED, 3),
        "RUNTS_PRACTICE_UPDATED": (NightlyTaskType.ADMIN_PRACTICE_UNRESOLVED, 5),
        "RUNTS_ATTACHMENT_DISCOVERED": (NightlyTaskType.COMPLEX_DOCUMENT, 7),
        "RUNTS_ACTION_REQUIRED": (NightlyTaskType.ADMIN_PRACTICE_UNRESOLVED, 2),
    }

    def __init__(self, queue: NightlyQueue) -> None:
        self.queue = queue

    def __call__(self, event: Any) -> NightlyJob:
        try:
            task_type, complexity = self.TASKS[event.event_type]
            source = event.provenance[0]
        except (KeyError, IndexError, AttributeError) as exc:
            raise ValueError("nightly_event_unsupported") from exc
        return self.queue.enqueue(
            task_type, {"event_id": event.event_id, "entity_refs": list(event.entity_refs)},
            source, complexity=complexity, now=event.observed_at,
        )


def _night_window(moment: datetime) -> bool:
    local = moment.astimezone(ROME).time()
    return local >= time(23, 0) or local <= time(7, 30)


def _reject_secrets(payload: Mapping[str, Any]) -> None:
    forbidden = {"password", "token", "authorization", "cookie", "api_key", "secret"}
    def walk(value: Any) -> None:
        if isinstance(value, Mapping):
            for key, nested in value.items():
                if str(key).casefold() in forbidden:
                    raise ValueError("nightly_payload_secret_forbidden")
                walk(nested)
        elif isinstance(value, (list, tuple)):
            for nested in value:
                walk(nested)
    walk(payload)


__all__ = ["ModelProvider", "ModelRoutingPolicy", "ModelRunResult", "ModelTier", "NightlyEventSink", "NightlyJob", "NightlyJobState", "NightlyQueue", "NightlyTaskType", "NightlyWorker"]
