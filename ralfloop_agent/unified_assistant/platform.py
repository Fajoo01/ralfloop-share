from __future__ import annotations

import math
import json
import re
import time
from collections import Counter
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Iterable

from pydantic import Field, model_validator

from .contracts import Identifier, StrictModel


class CapabilityPermission(StrEnum):
    READ = "READ"
    DRAFT = "DRAFT"
    PROPOSE = "PROPOSE"
    EXECUTE = "EXECUTE"
    ADMIN = "ADMIN"


class PromotionState(StrEnum):
    DISABLED = "DISABLED"
    READ_ONLY = "READ_ONLY"
    SHADOW = "SHADOW"
    APPROVAL_REQUIRED = "APPROVAL_REQUIRED"
    ACTIVE = "ACTIVE"


class ErrorCode(StrEnum):
    NOT_FOUND = "NOT_FOUND"
    AMBIGUOUS = "AMBIGUOUS"
    UNAUTHORIZED = "UNAUTHORIZED"
    FORBIDDEN = "FORBIDDEN"
    SOURCE_UNAVAILABLE = "SOURCE_UNAVAILABLE"
    RATE_LIMITED = "RATE_LIMITED"
    MALFORMED_RESPONSE = "MALFORMED_RESPONSE"
    INCOMPLETE_SOURCE = "INCOMPLETE_SOURCE"
    CONFLICT = "CONFLICT"
    STALE_DATA = "STALE_DATA"
    STALE_PROPOSAL = "STALE_PROPOSAL"
    EXECUTION_FAILED = "EXECUTION_FAILED"
    EXECUTION_UNVERIFIED = "EXECUTION_UNVERIFIED"


class SourceRef(StrictModel):
    system: Identifier
    native_id: str = Field(min_length=1, max_length=500)
    locator: str = Field(min_length=1, max_length=1000)
    observed_at: str = Field(min_length=1, max_length=64)
    content_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


class NormalizedError(StrictModel):
    code: ErrorCode
    message: str = Field(min_length=1, max_length=500)
    retryable: bool = False
    source: SourceRef | None = None


class CapabilityDescriptor(StrictModel):
    capability_id: Identifier
    server_id: Identifier
    domain: Identifier
    name: str = Field(min_length=1, max_length=160)
    description: str = Field(min_length=1, max_length=600)
    keywords: tuple[str, ...] = Field(default_factory=tuple, max_length=48)
    input_schema: dict[str, Any] = Field(default_factory=dict)
    output_schema: dict[str, Any] = Field(default_factory=dict)
    permission: CapabilityPermission
    side_effect: bool = False
    approval_required: bool = False
    source_system: Identifier
    version: str = Field(min_length=1, max_length=32)
    promotion: PromotionState = PromotionState.DISABLED
    enabled: bool = False
    health: str = Field(default="unknown", min_length=1, max_length=96)

    @model_validator(mode="after")
    def fail_closed(self) -> "CapabilityDescriptor":
        if self.permission in {CapabilityPermission.EXECUTE, CapabilityPermission.ADMIN} and not self.approval_required:
            raise ValueError("privileged_capability_requires_approval")
        if self.promotion is PromotionState.DISABLED and self.enabled:
            raise ValueError("disabled_capability_cannot_be_enabled")
        if self.permission is CapabilityPermission.READ and self.side_effect:
            raise ValueError("read_capability_cannot_have_side_effect")
        return self


class CapabilityResult(StrictModel):
    ok: bool
    capability_id: Identifier
    data: dict[str, Any] | None = None
    sources: tuple[SourceRef, ...] = ()
    error: NormalizedError | None = None

    @model_validator(mode="after")
    def result_shape(self) -> "CapabilityResult":
        if self.ok == (self.error is not None):
            raise ValueError("capability_result_invalid")
        return self


class CapabilityRegistry:
    def __init__(self, capabilities: Iterable[CapabilityDescriptor]) -> None:
        rows = tuple(capabilities)
        if len({row.capability_id for row in rows}) != len(rows):
            raise ValueError("duplicate_capability_id")
        self._rows = {row.capability_id: row for row in rows}

    def get(self, capability_id: str) -> CapabilityDescriptor:
        try:
            return self._rows[capability_id]
        except KeyError as exc:
            raise KeyError("unknown_capability_denied") from exc

    def list(self) -> tuple[CapabilityDescriptor, ...]:
        return tuple(self._rows[key] for key in sorted(self._rows))

    def retrieve(
        self, query: str, *, domains: Iterable[str] = (), allowed_permissions: Iterable[CapabilityPermission] = (CapabilityPermission.READ, CapabilityPermission.DRAFT, CapabilityPermission.PROPOSE), limit: int = 8,
    ) -> tuple[CapabilityDescriptor, ...]:
        if not 1 <= limit <= 12:
            raise ValueError("capability_retrieval_limit_invalid")
        terms = _terms(query)
        domain_filter = set(domains)
        permission_filter = set(allowed_permissions)
        candidates = [row for row in self._rows.values() if row.enabled and row.promotion is not PromotionState.DISABLED and row.health not in {"down", "unavailable"} and row.permission in permission_filter and (not domain_filter or row.domain in domain_filter)]
        document_frequency = Counter(term for row in candidates for term in set(_descriptor_terms(row)))
        ranked: list[tuple[float, str, CapabilityDescriptor]] = []
        for row in candidates:
            document = Counter(_descriptor_terms(row))
            score = sum(document[term] * (math.log((len(candidates) + 1) / (document_frequency[term] + 0.5)) + 1) for term in terms)
            if row.domain in terms:
                score += 3
            if score > 0:
                ranked.append((score, row.capability_id, row))
        ranked.sort(key=lambda item: (-item[0], item[1]))
        return tuple(item[2] for item in ranked[:limit])


@dataclass(frozen=True)
class RetrievalEvalCase:
    case_id: str
    query: str
    expected: frozenset[str]
    domains: tuple[str, ...] = ()


@dataclass(frozen=True)
class RetrievalEvalMetrics:
    cases: int
    recall_at_k: float
    precision_at_k: float
    irrelevant_tool_count: int
    schema_tokens_injected: int
    mean_selected_count: float
    latency_mean_ms: float
    latency_p95_ms: float
    task_success: float
    hallucinated_tools: int


def evaluate_retrieval(registry: CapabilityRegistry, cases: Iterable[RetrievalEvalCase], *, k: int = 8) -> RetrievalEvalMetrics:
    known = {row.capability_id for row in registry.list()}
    recalls: list[float] = []
    precisions: list[float] = []
    latencies: list[float] = []
    irrelevant = schema_tokens = selected_total = hallucinated = 0
    case_rows = tuple(cases)
    if not case_rows:
        raise ValueError("retrieval_eval_cases_required")
    for case in case_rows:
        started = time.perf_counter_ns()
        selected = registry.retrieve(case.query, domains=case.domains, limit=k)
        latencies.append((time.perf_counter_ns() - started) / 1_000_000)
        selected_ids = {row.capability_id for row in selected}
        relevant = len(selected_ids & case.expected)
        recalls.append(relevant / len(case.expected) if case.expected else float(not selected_ids))
        precisions.append(relevant / len(selected_ids) if selected_ids else float(not case.expected))
        irrelevant += len(selected_ids - case.expected)
        hallucinated += len(selected_ids - known)
        selected_total += len(selected_ids)
        schema_tokens += sum(_schema_tokens(row) for row in selected)
    ordered = sorted(latencies)
    p95_index = max(0, math.ceil(len(ordered) * 0.95) - 1)
    return RetrievalEvalMetrics(
        cases=len(case_rows), recall_at_k=sum(recalls) / len(recalls), precision_at_k=sum(precisions) / len(precisions),
        irrelevant_tool_count=irrelevant, schema_tokens_injected=schema_tokens,
        mean_selected_count=selected_total / len(case_rows), latency_mean_ms=sum(latencies) / len(latencies),
        latency_p95_ms=ordered[p95_index], task_success=sum(value == 1.0 for value in recalls) / len(recalls),
        hallucinated_tools=hallucinated,
    )


def _schema_tokens(row: CapabilityDescriptor) -> int:
    payload = json.dumps({"name": row.name, "description": row.description, "input": row.input_schema, "output": row.output_schema}, sort_keys=True, separators=(",", ":"))
    return math.ceil(len(payload.encode("utf-8")) / 4)


def _terms(text: str) -> tuple[str, ...]:
    return tuple(re.findall(r"[a-z0-9_]+", text.casefold()))


def _descriptor_terms(row: CapabilityDescriptor) -> tuple[str, ...]:
    return _terms(" ".join((row.capability_id, row.domain, row.name, row.description, *row.keywords)))


__all__ = ["CapabilityDescriptor", "CapabilityPermission", "CapabilityRegistry", "CapabilityResult", "ErrorCode", "NormalizedError", "PromotionState", "RetrievalEvalCase", "RetrievalEvalMetrics", "SourceRef", "evaluate_retrieval"]
