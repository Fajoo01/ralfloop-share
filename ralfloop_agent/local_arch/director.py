from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any, Callable, Mapping

from .contracts import CompactDirectorPlan, ContractError, WorkerDelta


DIRECTOR_MAX_TOKENS = 96
DIRECTOR_REPAIR_TOKENS = 32


@dataclass(frozen=True)
class DirectorContext:
    task: str
    artifact_refs: tuple[str, ...]
    verified_facts: tuple[Mapping[str, Any], ...]
    known_gaps: tuple[str, ...]
    deltas: tuple[WorkerDelta, ...]

    def compact_packet(self) -> dict[str, Any]:
        return {
            "v": 1,
            "task": self.task,
            "artifact_refs": list(self.artifact_refs),
            "verified_facts": list(self.verified_facts),
            "known_gaps": list(self.known_gaps),
            "deltas": [delta.as_dict() for delta in self.deltas],
        }


class DirectorAdapter:
    """Bounded strategy adapter: delta context, exact claim, one repair."""

    def __init__(self, generate: Callable[[str, int], str]):
        self.generate = generate

    def plan(self, context: DirectorContext) -> CompactDirectorPlan:
        prompt = json.dumps(context.compact_packet(), ensure_ascii=False, separators=(",", ":"))
        raw = self.generate(prompt, DIRECTOR_MAX_TOKENS)
        try:
            return self._parse(raw, context.task)
        except ContractError as first:
            repair = json.dumps({"error": str(first), "task": context.task, "bad": raw[:512]}, separators=(",", ":"))
            return self._parse(self.generate(repair, DIRECTOR_REPAIR_TOKENS), context.task)

    @staticmethod
    def _parse(raw: str, task: str) -> CompactDirectorPlan:
        stripped = raw.strip()
        if not stripped.startswith("{") or not stripped.endswith("}"):
            raise ContractError("director_prose_rejected")
        try:
            value = json.loads(stripped)
        except json.JSONDecodeError as exc:
            raise ContractError("director_invalid_json") from exc
        if any(key in value for key in ("properties", "$schema", "required")):
            raise ContractError("director_schema_echo")
        return CompactDirectorPlan.from_mapping(value, expected_task=task)
