from __future__ import annotations

from dataclasses import asdict, dataclass, field
import json
import math
import re
from typing import Any, Mapping, Sequence


ACTION_NAMES = {
    "ET": "existing_tool",
    "AF": "algorithm_factory",
    "SM": "specialist_model",
    "VR": "visual_rag",
    "AB": "audiobook",
    "SI": "social_image",
    "SV": "social_video",
    "MC": "media_compose",
    "LM": "large_model",
    "AP": "ask_approval",
    "AU": "ask_user",
    "FN": "finish",
    "RJ": "reject",
}
ROUTE_KEYS = frozenset({"v", "a", "t", "i", "k", "c", "r"})
_ATOM = re.compile(r"^[a-zA-Z0-9_.:/-]{1,96}$")
_REASON = re.compile(r"^[A-Z0-9_]{1,48}$")


class ContractError(ValueError):
    pass


def _compact_list(value: Any, field_name: str, *, limit: int = 12) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or len(value) > limit:
        raise ContractError(f"invalid_{field_name}")
    result: list[str] = []
    for item in value:
        if not isinstance(item, str) or not _ATOM.fullmatch(item):
            raise ContractError(f"invalid_{field_name}")
        result.append(item)
    return tuple(result)


@dataclass(frozen=True)
class CompactRoute:
    v: int
    a: str
    t: str
    i: tuple[str, ...] = ()
    k: tuple[str, ...] = ()
    c: float = 0.0
    r: str | None = None

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "CompactRoute":
        unknown = set(value) - ROUTE_KEYS
        if unknown:
            raise ContractError("unknown_route_fields")
        if value.get("v") != 1:
            raise ContractError("invalid_route_version")
        action = value.get("a")
        if not isinstance(action, str) or "|" in action or action not in ACTION_NAMES:
            raise ContractError("invalid_action")
        target = value.get("t", "none")
        if not isinstance(target, str) or not _ATOM.fullmatch(target):
            raise ContractError("invalid_target")
        confidence = value.get("c")
        if not isinstance(confidence, (float, int)) or isinstance(confidence, bool):
            raise ContractError("invalid_confidence")
        confidence = float(confidence)
        if not math.isfinite(confidence) or not 0.0 <= confidence <= 1.0:
            raise ContractError("invalid_confidence")
        reason = value.get("r")
        if reason is not None and (not isinstance(reason, str) or not _REASON.fullmatch(reason)):
            raise ContractError("invalid_reason")
        return cls(
            v=1,
            a=action,
            t=target,
            i=_compact_list(value.get("i"), "inputs"),
            k=_compact_list(value.get("k"), "constraints"),
            c=confidence,
            r=reason,
        )

    @classmethod
    def parse_model_output(cls, raw: str, *, max_bytes: int = 1024) -> "CompactRoute":
        stripped = raw.strip()
        if not stripped or len(stripped.encode("utf-8")) > max_bytes:
            raise ContractError("route_size")
        if not stripped.startswith("{") or not stripped.endswith("}"):
            raise ContractError("prose_rejected")
        try:
            value = json.loads(stripped)
        except json.JSONDecodeError as exc:
            raise ContractError("invalid_json") from exc
        if not isinstance(value, dict):
            raise ContractError("route_not_object")
        if any(key in value for key in ("properties", "required", "$schema", "definitions")):
            raise ContractError("schema_echo")
        return cls.from_mapping(value)

    def as_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {"v": 1, "a": self.a, "t": self.t}
        if self.i:
            result["i"] = list(self.i)
        if self.k:
            result["k"] = list(self.k)
        result["c"] = round(self.c, 4)
        if self.r:
            result["r"] = self.r
        return result

    def compact_json(self) -> str:
        return json.dumps(self.as_dict(), ensure_ascii=False, separators=(",", ":"), sort_keys=False)


@dataclass(frozen=True)
class ToolRecord:
    compact_id: str
    name: str
    category: str
    capabilities: tuple[str, ...]
    input_schema_hash: str
    output_schema_hash: str
    side_effect: bool
    network_requirement: str
    approval_requirement: str
    runtime: str
    model: str | None
    availability: str
    cost_class: str
    latency_class: str
    trust_level: str
    provenance_policy: str

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "ToolRecord":
        required = set(cls.__dataclass_fields__)
        if set(value) != required:
            raise ContractError("invalid_tool_record_fields")
        compact_id = value["compact_id"]
        if compact_id not in ACTION_NAMES:
            raise ContractError("invalid_tool_compact_id")
        capabilities = value["capabilities"]
        if not isinstance(capabilities, list) or not capabilities:
            raise ContractError("invalid_tool_capabilities")
        payload = dict(value)
        payload["capabilities"] = tuple(str(item) for item in capabilities)
        return cls(**payload)


@dataclass(frozen=True)
class WorkerDelta:
    v: int
    task: str
    status: str
    facts: tuple[Mapping[str, Any], ...] = ()
    issues: tuple[Mapping[str, Any], ...] = ()
    metrics: tuple[Mapping[str, Any], ...] = ()
    artifacts: tuple[str, ...] = ()
    confidence: float = 0.0

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "WorkerDelta":
        if value.get("v") != 1:
            raise ContractError("invalid_delta_version")
        task = value.get("task")
        status = value.get("status")
        if not isinstance(task, str) or not _ATOM.fullmatch(task):
            raise ContractError("invalid_delta_task")
        if status not in {"ok", "partial", "failed", "blocked"}:
            raise ContractError("invalid_delta_status")
        confidence = value.get("confidence", 0.0)
        if not isinstance(confidence, (float, int)) or not 0 <= float(confidence) <= 1:
            raise ContractError("invalid_delta_confidence")
        facts = tuple(value.get("facts", ()))
        issues = tuple(value.get("issues", ()))
        metrics = tuple(value.get("metrics", ()))
        artifacts = tuple(value.get("artifacts", ()))
        if not all(isinstance(item, Mapping) for items in (facts, issues, metrics) for item in items):
            raise ContractError("invalid_delta_items")
        if not all(isinstance(item, str) and item.startswith("sha256:") for item in artifacts):
            raise ContractError("invalid_delta_artifacts")
        return cls(1, task, status, facts, issues, metrics, artifacts, float(confidence))

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class DirectorAssignment:
    task: str
    capability: str
    input_refs: tuple[str, ...] = ()


@dataclass(frozen=True)
class CompactDirectorPlan:
    version: int
    exact_task: str
    assignments: tuple[DirectorAssignment, ...]
    artifact_refs: tuple[str, ...] = ()
    verified_facts: tuple[Mapping[str, Any], ...] = ()
    known_gaps: tuple[str, ...] = ()

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any], *, expected_task: str) -> "CompactDirectorPlan":
        if value.get("v") != 1 or value.get("task") != expected_task:
            raise ContractError("director_exact_task_mismatch")
        raw_assignments = value.get("assignments", [])
        if not isinstance(raw_assignments, list) or len(raw_assignments) > 3:
            raise ContractError("director_assignment_limit")
        assignments: list[DirectorAssignment] = []
        for raw in raw_assignments:
            if not isinstance(raw, Mapping) or set(raw) != {"task", "capability", "input_refs"}:
                raise ContractError("invalid_director_assignment")
            capability = raw["capability"]
            if not isinstance(capability, str) or "|" in capability:
                raise ContractError("invalid_director_capability")
            assignments.append(DirectorAssignment(raw["task"], capability, tuple(raw["input_refs"])))
        return cls(
            version=1,
            exact_task=expected_task,
            assignments=tuple(assignments),
            artifact_refs=tuple(value.get("artifact_refs", ())),
            verified_facts=tuple(value.get("verified_facts", ())),
            known_gaps=tuple(value.get("known_gaps", ())),
        )
