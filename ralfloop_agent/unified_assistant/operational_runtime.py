from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Callable, Iterable, Mapping

from .bandi_service import BandiService, SourceAdapter
from .event_router import EventRouter
from .media_quality import MediaQualityService
from .memory_service import MemoryService
from .nightly_worker import (
    ModelProvider, ModelTier, NightlyEventSink, NightlyJob, NightlyQueue,
    NightlyTaskType, NightlyWorker,
)
from .observability import OperationalMetrics


class BottazziOperationalRuntime:
    """Local/shadow composition root. It owns no scheduler and performs no external write."""

    def __init__(
        self, memory_path: str | Path, *, providers: Mapping[ModelTier, ModelProvider] | None = None,
        deterministic_handlers: Mapping[NightlyTaskType, Callable[[NightlyJob], dict | None]] | None = None,
    ) -> None:
        self.metrics = OperationalMetrics()
        self.memory = MemoryService(memory_path)
        self.nightly_queue = NightlyQueue(self.memory)
        self.nightly_sink = NightlyEventSink(self.nightly_queue)
        self.events = EventRouter(
            self.memory,
            enabled_workflows=("bandi.review", "media.triage"),
            workflow_handlers={
                "bandi.review": self.nightly_sink,
                "media.triage": self.nightly_sink,
            },
            wake_handler=self.nightly_sink,
            metrics=self.metrics,
        )
        self.bandi = BandiService(self.memory, metrics=self.metrics, router=self.events)
        self.media = MediaQualityService(self.memory, router=self.events, metrics=self.metrics)
        self.nightly = NightlyWorker(
            self.nightly_queue, providers or {},
            deterministic_handlers=deterministic_handlers, metrics=self.metrics,
        )

    def poll_bandi(self, adapters: Iterable[SourceAdapter]):
        return self.bandi.poll(adapters)

    def run_nightly_once(self, *, now: datetime | None = None, force: bool = False):
        return self.nightly.run_once(now=now, force=force)

    def close(self) -> None:
        self.memory.close()

    def __enter__(self) -> "BottazziOperationalRuntime":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


__all__ = ["BottazziOperationalRuntime"]
