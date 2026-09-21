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
        "abc_propose_event",
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
PROPOSAL_TRIGGERS = (
    "abc:",
    "aggiorna abc",
    "proponi evento abc",
    "registra evento relazionale",
)


@dataclass(frozen=True)
class ABCReadResolution:
    available: bool
    context: Mapping[str, Any]
    fallback_skills: tuple[str, ...] = ()
    error: str | None = None

    def render(self, *, max_chars: int = 32768) -> str:
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


def render_abc_relation_answer(user_goal: str, resolution: ABCReadResolution) -> str:
    """Render a concise human-facing answer without turning scores into probabilities."""
    if not resolution.available:
        return "ABC Relation MCP non disponibile; il percorso canonico read-only non ha prodotto evidenza."

    context = resolution.context
    analysis = context.get("analysis") if isinstance(context, Mapping) else None
    if not isinstance(analysis, Mapping):
        analysis = {}
    relcalc = analysis.get("relcalc")
    if not isinstance(relcalc, Mapping):
        relcalc = analysis

    score = relcalc.get("score")
    confidence = relcalc.get("confidence")
    evidence_count = relcalc.get("evidence_count", analysis.get("active_event_count"))
    action = relcalc.get("next_safe_action")

    lines: list[str] = []
    proposal = context.get("proposal") if isinstance(context, Mapping) else None
    if isinstance(proposal, Mapping):
        events = proposal.get("events")
        event = events[0] if isinstance(events, list) and events and isinstance(events[0], Mapping) else {}
        if event:
            lines.append(
                f"Proposta ABC non registrata: [{event.get('kind', 'unknown')}] {event.get('summary', '')}."
            )
            lines.append(f"Digest proposta: {proposal.get('proposal_digest', 'n/d')}.")
            if (proposal.get("review") or {}).get("requires_human_review"):
                lines.append("La classificazione è interpretativa e richiede revisione umana prima di qualunque commit.")
            else:
                lines.append("Nessuna scrittura eseguita: il commit resta separato e richiede conferma del digest.")
    if score is not None:
        line = f"Curva canonica ABC: {score}/100"
        if evidence_count is not None:
            line += f" su {evidence_count} eventi"
        if confidence is not None:
            line += f"; confidence interna {confidence}"
        lines.append(line + ".")
        lines.append(
            "Lo score è un indicatore tecnico rispetto al neutro 50: non è una percentuale di successo "
            "e la confidence misura la qualità/coerenza dell'evidenza registrata, non la certezza sulle intenzioni di Arianna."
        )

    action_labels = {
        "light_non_pressing_presence": "presenza leggera e non pressante",
        "do_nothing_active": "non forzare e lasciare maturare i dati",
        "collect_observable_evidence": "raccogliere solo evidenze osservabili, evitando letture mentali o segnali intrusivi",
    }
    if action in action_labels:
        lines.append(f"Indicazione operativa del calcolatore: {action_labels[action]}.")

    timeline = context.get("timeline") if isinstance(context, Mapping) else None
    if isinstance(timeline, list) and timeline:
        lines.append("Ultimi eventi canonici:")
        for event in timeline[-5:]:
            if not isinstance(event, Mapping):
                continue
            when = str(event.get("occurred_at") or "").split("T", 1)[0]
            summary = str(event.get("summary") or event.get("event") or "").strip()
            if summary:
                lines.append(f"- {when}: {summary}" if when else f"- {summary}")

    references = context.get("references") if isinstance(context, Mapping) else None
    if isinstance(references, list) and references:
        frameworks = [str(row.get("framework")) for row in references if isinstance(row, Mapping) and row.get("framework")]
        if frameworks:
            lines.append("Riferimenti caricati: " + ", ".join(frameworks) + ".")
            lines.append("Sono euristiche per interpretazione e comunicazione, non prove di stati mentali o intenzioni nascoste.")

    return "\n".join(lines) if lines else "ABC Relation MCP: evidenza read-only raccolta."


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
            if any(trigger in goal for trigger in PROPOSAL_TRIGGERS):
                context["proposal"] = client.propose(user_goal)
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
    "render_abc_relation_answer",
]
