from __future__ import annotations

import json
import os
import re

from ralfloop_agent.integration.bottazzi_motor_judge import BotTazziMotorJudge

from .agenda import AgendaCandidate, AgendaKind, AgendaSource, AmbiguityResolver


_JSON_RE = re.compile(r"\{.*\}", re.S)


class MotorAgendaResolver(AmbiguityResolver):
    """Heavy ambiguity resolver. It has no side-effect authority."""

    def __init__(self, motor: BotTazziMotorJudge | None = None) -> None:
        self.motor = motor or BotTazziMotorJudge()

    def resolve(self, source: AgendaSource, candidate: AgendaCandidate) -> AgendaCandidate:
        prompt = (
            "Classifica il testo per Bot-tazzi Agenda. Non inventare date o impegni. "
            "Rispondi solo JSON con chiavi kind e confidence. kind deve essere uno tra "
            "task, appointment, notification, information. Se il testo contiene incertezza "
            "su un appuntamento, usa information.\n"
            f"Canale: {source.channel}\nMittente: {source.sender}\n"
            f"Testo originale: {source.original_text}\n"
            f"Classificazione fast-lane: {candidate.kind.value}; uncertain={candidate.uncertain}."
        )
        try:
            raw = self.motor.complete(
                [
                    {"role": "system", "content": "Sei il resolver semantico di Bot-tazzi Agenda. Output JSON soltanto."},
                    {"role": "user", "content": prompt},
                ],
                max_tokens=80,
                task_id=f"agenda-{source.native_id}"[:64],
                mode="agenda_ambiguity",
            )
            match = _JSON_RE.search(raw)
            if match is None:
                return candidate
            payload = json.loads(match.group(0))
            kind = AgendaKind(str(payload.get("kind") or "information"))
            confidence = float(payload.get("confidence") or candidate.confidence)
        except (RuntimeError, ValueError, TypeError, json.JSONDecodeError):
            return candidate
        confidence = max(0.0, min(1.0, confidence))
        # Motor can refine semantics but cannot remove explicit source uncertainty.
        if candidate.uncertain and kind is AgendaKind.APPOINTMENT:
            kind = AgendaKind.INFORMATION
        return candidate.model_copy(update={
            "kind": kind,
            "confidence": confidence,
            "needs_motor": False,
        })


def agenda_ambiguity_resolver_from_env() -> MotorAgendaResolver | None:
    enabled = os.getenv("BOTTAZZI_AGENDA_MOTOR_ENABLED", "1").casefold() not in {"0", "false", "no", "off"}
    return MotorAgendaResolver() if enabled else None


__all__ = ["MotorAgendaResolver", "agenda_ambiguity_resolver_from_env"]
