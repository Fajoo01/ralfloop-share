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
from .pec_runts import PecReadProvider, PecRuntsService, RuntsReadProvider
from .pec_runts_mcp import PecRuntsMCPServer, capability_descriptors
from .platform import CapabilityRegistry
from .runtsuite_adapter import RuntsuiteReadOnlyAdapter


class BottazziOperationalRuntime:
    """Local/shadow composition root. It owns no scheduler and performs no external write."""

    def __init__(
        self, memory_path: str | Path, *, providers: Mapping[ModelTier, ModelProvider] | None = None,
        deterministic_handlers: Mapping[NightlyTaskType, Callable[[NightlyJob], dict | None]] | None = None,
        pec_provider: PecReadProvider | None = None,
        runts_provider: RuntsReadProvider | None = None,
        runtsuite_provider: RuntsuiteReadOnlyAdapter | None = None,
        runts_response_preparer=None,
    ) -> None:
        self.metrics = OperationalMetrics()
        self.memory = MemoryService(memory_path)
        self.nightly_queue = NightlyQueue(self.memory)
        self.nightly_sink = NightlyEventSink(self.nightly_queue)
        self.events = EventRouter(
            self.memory,
            enabled_workflows=("bandi.review", "media.triage", "runts.correlate", "runts.review"),
            workflow_handlers={
                "bandi.review": self.nightly_sink,
                "media.triage": self.nightly_sink,
                "runts.correlate": self.nightly_sink,
                "runts.review": self.nightly_sink,
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
        self.pec_runts = PecRuntsService(self.memory, pec_provider, runts_provider, runtsuite=runtsuite_provider, router=self.events, response_preparer=runts_response_preparer) if pec_provider and runts_provider else None
        self.pec_runts_mcp = PecRuntsMCPServer(self.pec_runts) if self.pec_runts else None
        self.pec_runts_capabilities = CapabilityRegistry(capability_descriptors())

    def poll_bandi(self, adapters: Iterable[SourceAdapter]):
        return self.bandi.poll(adapters)

    def run_nightly_once(self, *, now: datetime | None = None, force: bool = False):
        return self.nightly.run_once(now=now, force=force)

    def invoke_pec_runts(self, query: str, arguments: Mapping[str, object]):
        if self.pec_runts_mcp is None:
            return {"isError": True, "structuredContent": {"ok": False, "status": "SOURCE_UNAVAILABLE", "writes": 0}}
        selected = self.pec_runts_capabilities.retrieve(query, domains=("pec_runts",), limit=3)
        if not selected:
            return {"isError": True, "structuredContent": {"ok": False, "status": "POLICY_DENIED", "writes": 0}}
        for capability in selected:
            schema = capability.input_schema
            properties, required = set((schema.get("properties") or {}).keys()), set(schema.get("required") or ())
            if required <= set(arguments) <= properties:
                result = self.pec_runts_mcp.call(capability.capability_id, arguments)
                result["selectedCapability"] = capability.capability_id
                return result
        return {"isError": True, "structuredContent": {"ok": False, "status": "AMBIGUOUS", "candidates": [row.capability_id for row in selected], "writes": 0}}

    def close(self) -> None:
        self.memory.close()

    def __enter__(self) -> "BottazziOperationalRuntime":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


__all__ = ["BottazziOperationalRuntime"]
