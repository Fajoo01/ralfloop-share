from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence

from pydantic import BaseModel, ConfigDict, Field

from .contracts import (
    DomainSpec,
    MemoryItem,
    MemoryNamespace,
    MemoryProvenance,
    MemoryType,
)


_SECRET_RE = re.compile(
    r"(?i)(authorization|bearer|cookie|credential|password|secret|token)\s*[:=]\s*\S+"
)


class MemoryExclusion(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    item_id: str
    namespace: str
    reason: str


class MemoryTrace(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    active_domain: str
    allowed_namespaces: tuple[str, ...]
    requested_namespaces: tuple[str, ...]
    retrieved_items: tuple[dict, ...]
    excluded_items: tuple[MemoryExclusion, ...]
    working_context: tuple[dict, ...]


@dataclass(frozen=True)
class MemoryRetrieval:
    items: tuple[MemoryItem, ...]
    trace: MemoryTrace


@dataclass(frozen=True)
class MemoryWriteDecision:
    allowed: bool
    persistent: bool
    reason: str


class MemoryRouter:
    """Domain-scoped minimum retrieval. The caller cannot widen scope."""

    def __init__(self, items: Iterable[MemoryItem] = ()) -> None:
        values = tuple(items)
        if len({item.id for item in values}) != len(values):
            raise ValueError("duplicate_memory_item_id")
        self.items = values

    def retrieve(
        self,
        domain: DomainSpec,
        *,
        requested_namespaces: Sequence[str | MemoryNamespace] | None = None,
        subject: str | None = None,
        query_terms: Sequence[str] = (),
        memory_types: Sequence[MemoryType] | None = None,
        facts_only: bool = False,
        limit: int = 20,
    ) -> MemoryRetrieval:
        if not 1 <= limit <= 100:
            raise ValueError("memory_limit_invalid")
        allowed = set(domain.allowed_memory_namespaces)
        requested = (
            {_namespace(value) for value in requested_namespaces}
            if requested_namespaces is not None
            else set()
        )
        effective = requested & allowed
        wanted_types = set(memory_types or ())
        superseded_ids = {
            old_id for item in self.items if item.current for old_id in item.supersedes
        }
        included: list[MemoryItem] = []
        excluded: list[MemoryExclusion] = []
        for item in self.items:
            reason = self._exclusion_reason(
                item,
                allowed=allowed,
                requested=requested,
                effective=effective,
                superseded_ids=superseded_ids,
                subject=subject,
                query_terms=query_terms,
                wanted_types=wanted_types,
                facts_only=facts_only,
            )
            if reason:
                excluded.append(MemoryExclusion(
                    item_id=item.id, namespace=item.namespace.value, reason=reason
                ))
            else:
                included.append(item)
        included.sort(key=lambda item: (item.timestamp, item.id), reverse=True)
        for item in included[limit:]:
            excluded.append(MemoryExclusion(
                item_id=item.id, namespace=item.namespace.value, reason="limit_exceeded"
            ))
        included = included[:limit]
        trace = MemoryTrace(
            active_domain=domain.id,
            allowed_namespaces=tuple(sorted(item.value for item in allowed)),
            requested_namespaces=tuple(sorted(item.value for item in requested)),
            retrieved_items=tuple(_inspect_item(item) for item in included),
            excluded_items=tuple(excluded),
            working_context=tuple({
                "id": item.id,
                "namespace": item.namespace.value,
                "subject": item.subject,
                "slot": item.slot,
                "content": _redact(item.content),
                "certainty": item.certainty,
                "epistemic_kind": item.epistemic_kind,
                "derived": item.derived,
                "source_refs": list(item.source_refs),
                "content_role": "data",
            } for item in included),
        )
        return MemoryRetrieval(tuple(included), trace)

    @staticmethod
    def _exclusion_reason(
        item: MemoryItem,
        *,
        allowed: set[MemoryNamespace],
        requested: set[MemoryNamespace],
        effective: set[MemoryNamespace],
        superseded_ids: set[str],
        subject: str | None,
        query_terms: Sequence[str],
        wanted_types: set[MemoryType],
        facts_only: bool,
    ) -> str | None:
        if item.namespace not in allowed:
            return "namespace_not_allowed_for_domain"
        if item.namespace not in requested:
            return "namespace_not_requested"
        if item.namespace not in effective:
            return "namespace_scope_rejected"
        if not item.current or item.id in superseded_ids:
            return "superseded_or_not_current"
        if item.memory_type is MemoryType.CONVERSATION:
            return "conversation_not_retrieved_as_memory"
        if wanted_types and item.memory_type not in wanted_types:
            return "memory_type_not_requested"
        if facts_only and (item.derived or item.certainty in {"hypothesis", "calculated"}):
            return "derived_not_fact"
        if subject and item.subject.casefold() != subject.casefold():
            return "subject_mismatch"
        if query_terms:
            haystack = " ".join((item.subject, item.slot, item.content, *item.source_refs)).casefold()
            if not all(term.strip().casefold() in haystack for term in query_terms if term.strip()):
                return "query_mismatch"
        return None


class MemoryWritePolicy:
    """Pure gate. Persistence is delegated to the existing source store."""

    @staticmethod
    def authorize(item: MemoryItem, *, event_verified: bool, llm_generated: bool = False) -> MemoryWriteDecision:
        if item.memory_type in {MemoryType.WORKING, MemoryType.CONVERSATION}:
            return MemoryWriteDecision(False, False, "ephemeral_memory_not_persistent")
        if llm_generated and not item.derived:
            return MemoryWriteDecision(False, False, "llm_output_cannot_be_fact")
        if item.derived and item.certainty not in {"hypothesis", "calculated", "uncertain"}:
            return MemoryWriteDecision(False, False, "derived_item_cannot_be_verified_fact")
        if not event_verified and not item.derived:
            return MemoryWriteDecision(False, False, "unverified_event_cannot_persist_fact")
        if not item.source_refs:
            return MemoryWriteDecision(False, False, "source_refs_required")
        return MemoryWriteDecision(True, True, "verified_or_explicitly_derived")


def apply_supersession(existing: Sequence[MemoryItem], incoming: MemoryItem) -> tuple[tuple[MemoryItem, ...], MemoryItem]:
    prior = [
        item for item in existing
        if item.current
        and item.namespace is incoming.namespace
        and item.subject.casefold() == incoming.subject.casefold()
        and item.slot.casefold() == incoming.slot.casefold()
    ]
    old_ids = tuple(item.id for item in prior if item.id != incoming.id)
    updated = tuple(
        item.model_copy(update={"current": False}) if item.id in old_ids else item
        for item in existing
    )
    merged_supersedes = tuple(dict.fromkeys((*incoming.supersedes, *old_ids)))
    return updated, incoming.model_copy(update={"supersedes": merged_supersedes, "current": True})


def tiremm_profile_items(path: str | Path, organization: str = "Tiremm Innanz APS") -> tuple[MemoryItem, ...]:
    profile_path = Path(path)
    payload = json.loads(profile_path.read_text(encoding="utf-8"))
    profile = (payload.get("organizations") or {}).get(organization) or {}
    facts = profile.get("relevant_facts") or []
    result: list[MemoryItem] = []
    for index, fact in enumerate(facts):
        text = " ".join(str(fact).split())
        if not text:
            continue
        result.append(MemoryItem(
            id=f"tiremm.profile.{index}",
            namespace=MemoryNamespace.TIREMM,
            memory_type=MemoryType.LONG_TERM,
            subject=organization,
            slot=f"profile_fact_{index}",
            content=text,
            epistemic_kind="fact",
            timestamp="1970-01-01T00:00:00Z",
            provenance=MemoryProvenance.DOCUMENT,
            certainty="verified",
            source_refs=(f"{profile_path.name}#organizations/{organization}/relevant_facts/{index}",),
        ))
    return tuple(result)


def abc_memory_items(base: str | Path) -> tuple[MemoryItem, ...]:
    """Read-only adapter: never initializes or mutates the ABC store."""

    root = Path(base)
    result: list[MemoryItem] = []
    for index, event in enumerate(_read_jsonl(root / "events.jsonl")):
        event_id = _safe_id(str(event.get("id") or f"event-{index}"), "abc.event")
        event_type = str(event.get("type") or "observed_fact")
        certainty = "verified" if event_type == "observed_fact" else "reported"
        epistemic_kind = (
            "observation" if event_type == "observed_fact"
            else "reported_statement" if event_type == "message"
            else "event"
        )
        result.append(MemoryItem(
            id=event_id,
            namespace=MemoryNamespace.PERSONAL_RELATIONAL,
            memory_type=MemoryType.EPISODIC,
            subject=str(event.get("subject") or "relational_context"),
            slot=event_type,
            content=_bounded_content(event.get("text") or "(empty event)"),
            epistemic_kind=epistemic_kind,
            timestamp=str(event.get("created_at") or event.get("date") or "1970-01-01"),
            provenance=MemoryProvenance.USER_STATEMENT,
            certainty=certainty,
            source_refs=(f"{root / 'events.jsonl'}#{index}",),
        ))
    hypotheses = _read_json(root / "hypotheses.json", {})
    for key, value in hypotheses.items():
        if not isinstance(value, dict):
            continue
        result.append(MemoryItem(
            id=_safe_id(str(key), "abc.hypothesis"),
            namespace=MemoryNamespace.PERSONAL_RELATIONAL,
            memory_type=MemoryType.LONG_TERM,
            subject="relational_context",
            slot=str(key)[:96],
            content=_bounded_content(value.get("notes") or key),
            epistemic_kind="hypothesis",
            timestamp=str(value.get("last_updated") or "1970-01-01"),
            provenance=MemoryProvenance.DERIVED_CALCULATION,
            certainty="hypothesis",
            source_refs=(str(root / "hypotheses.json"),),
            derived=True,
            current=str(value.get("status") or "active") == "active",
        ))
    state = _read_json(root / "state.json", {})
    if state:
        result.append(MemoryItem(
            id="abc.calculated.state",
            namespace=MemoryNamespace.PERSONAL_RELATIONAL,
            memory_type=MemoryType.LONG_TERM,
            subject="relational_context",
            slot="calculated_state",
            content=_bounded_content(json.dumps(state, ensure_ascii=False, sort_keys=True)),
            epistemic_kind="derived_score",
            timestamp=str(state.get("last_updated") or "1970-01-01"),
            provenance=MemoryProvenance.DERIVED_CALCULATION,
            certainty="calculated",
            source_refs=(str(root / "state.json"),),
            derived=True,
        ))
    return tuple(result)


def _namespace(value: str | MemoryNamespace) -> MemoryNamespace:
    try:
        return value if isinstance(value, MemoryNamespace) else MemoryNamespace(str(value))
    except ValueError as exc:
        raise ValueError("unknown_memory_namespace") from exc


def _inspect_item(item: MemoryItem) -> dict:
    return {
        "id": item.id,
        "namespace": item.namespace.value,
        "memory_type": item.memory_type.value,
        "subject": item.subject,
        "slot": item.slot,
        "provenance": item.provenance.value,
        "certainty": item.certainty,
        "epistemic_kind": item.epistemic_kind,
        "source_refs": list(item.source_refs),
        "current": item.current,
        "derived": item.derived,
    }


def _redact(value: str) -> str:
    return _SECRET_RE.sub(lambda match: match.group(1) + "=[REDACTED]", value)


def _bounded_content(value: object) -> str:
    text = " ".join(str(value).split())
    return text[:4000] or "(empty)"


def _safe_id(value: str, prefix: str) -> str:
    folded = re.sub(r"[^a-z0-9_.-]+", "-", value.casefold()).strip("-.")
    candidate = f"{prefix}.{folded}"[:96]
    if re.fullmatch(r"[a-z][a-z0-9_.-]{0,95}", candidate):
        return candidate
    digest = hashlib.sha256(value.encode()).hexdigest()[:16]
    return f"{prefix}.{digest}"


def _read_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def _read_jsonl(path: Path) -> list[dict]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    result: list[dict] = []
    for line in lines:
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            result.append(value)
    return result


__all__ = [
    "MemoryRetrieval",
    "MemoryRouter",
    "MemoryTrace",
    "MemoryWriteDecision",
    "MemoryWritePolicy",
    "abc_memory_items",
    "apply_supersession",
    "tiremm_profile_items",
]
