from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable, Mapping

from ralfloop_agent.abc_relation.client import RelationMCPClient


READ_ONLY_TOOLS = frozenset(
    {
        "abc_get_state",
        "abc_get_timeline",
        "abc_search_events",
        "abc_analyze",
        "abc_explain_event",
        "abc_list_snapshots",
        "abc_get_reference_library",
        "abc_policy_status",
    }
)
WRITE_TOOLS = frozenset({"abc_record_event", "abc_create_snapshot"})
LEGACY_FALLBACK_SKILLS = ("abc_memory", "abc_relcalc")
REFERENCE_TRIGGERS = (
    "manuale",
    "manuali",
    "psicologia",
    "dialogo strategico",
    "motivational interviewing",
    "investment model",
    "interdipendenza",
    "attaccamento",
    "dialogo",
    "comunicazione",
    "cosa dire",
    "cosa scrivere",
    "come rispondere",
    "messaggio",
)
TIMELINE_TRIGGERS = ("timeline", "cronologia", "ultimi eventi", "eventi recenti")


@dataclass(frozen=True)
class ABCReadResolution:
    available: bool
    context: Mapping[str, Any]
    fallback_skills: tuple[str, ...] = ()
    error: str | None = None

    def render(self, *, max_chars: int = 12000) -> str:
        payload = {
            "source": "abc_relation_mcp_read_only",
            "available": self.available,
            "context": self.context,
            "fallback_skills": list(self.fallback_skills),
        }
        if self.error:
            payload["error"] = self.error
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
        if len(encoded) <= max_chars:
            return encoded
        return encoded[: max_chars - 32] + "…[context truncated]"


class ABCRelationReadAdapter:
    """Resolve ABC context through a strictly read-only MCP surface."""

    def __init__(
        self,
        client_factory: Callable[[], RelationMCPClient] | None = None,
    ) -> None:
        self._client_factory = client_factory or RelationMCPClient

    def resolve(self, user_goal: str) -> ABCReadResolution:
        goal = user_goal.casefold()
        try:
            client = self._client_factory()
            state = client.get_state()
            context: dict[str, Any] = {
                "state": state,
                "analysis": client.analyze(),
            }
            if any(trigger in goal for trigger in TIMELINE_TRIGGERS):
                timeline = client.timeline(limit=30)
                if not timeline and isinstance(state, Mapping):
                    snapshot = state.get("snapshot")
                    if isinstance(snapshot, Mapping):
                        legacy_timeline = snapshot.get("legacy_timeline")
                        if isinstance(legacy_timeline, (list, tuple)):
                            timeline = list(legacy_timeline)[-30:]
                context["timeline"] = timeline
            if any(trigger in goal for trigger in REFERENCE_TRIGGERS):
                context["references"] = client.references()
            return ABCReadResolution(available=True, context=context)
        except (OSError, TimeoutError, RuntimeError) as exc:
            return ABCReadResolution(
                available=False,
                context={},
                fallback_skills=LEGACY_FALLBACK_SKILLS,
                error=_bounded_error(exc),
            )


def _bounded_error(exc: Exception) -> str:
    name = type(exc).__name__
    detail = str(exc).replace("\n", " ").strip()
    if not detail:
        return name
    return f"{name}: {detail[:240]}"


__all__ = [
    "ABCReadResolution",
    "ABCRelationReadAdapter",
    "LEGACY_FALLBACK_SKILLS",
    "READ_ONLY_TOOLS",
    "WRITE_TOOLS",
]
