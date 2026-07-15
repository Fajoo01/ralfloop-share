from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any, Iterable, Mapping

from .domain_opinion import DomainReasoningInput, validate_domain_opinion
from .recursive_mas_domain_serialization import (
    DEMO_RULE_ID,
    DEMO_SOURCE_ID,
    DomainSerializationError,
    ID_MENTION_RE,
    MAX_TEXT_LENGTH,
)


PROTOCOL_VERSION = "domain_evidence_packet_v1"
SOLVER_FIELDS = {
    "position",
    "support",
    "counter",
    "uncertainty",
    "alternatives",
    "recommendation",
    "confidence",
    "human",
}
DEMO_MODES = {"none", "skeleton", "invalid_ids"}
CANONICAL_ORDER = {
    "POSITION": 0,
    "SUPPORT": 1,
    "COUNTER": 2,
    "UNCERTAINTY": 3,
    "ALTERNATIVE": 4,
    "RECOMMENDATION": 5,
    "CONFIDENCE": 6,
    "HUMAN": 7,
}


def _deduplicate(values: Iterable[str]) -> tuple[str, ...]:
    output: list[str] = []
    for value in values:
        item = str(value).strip()
        if item and item not in output:
            output.append(item)
    return tuple(output)


def _bounded_text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise DomainSerializationError("solver_semantics_incomplete")
    text = value.strip()
    if len(text) > MAX_TEXT_LENGTH:
        raise DomainSerializationError(f"{field}_too_long")
    return text


def _bounded_strings(value: Any, field: str, limit: int) -> tuple[str, ...]:
    if not isinstance(value, list) or len(value) > limit:
        raise DomainSerializationError(f"{field}_list_invalid")
    return tuple(_bounded_text(item, field) for item in value)


def _validate_ids(values: Iterable[str], allowed: tuple[str, ...], field: str) -> tuple[str, ...]:
    normalized = _deduplicate(values)
    allowed_set = set(allowed)
    for identifier in normalized:
        if identifier in {DEMO_RULE_ID, DEMO_SOURCE_ID} or identifier not in allowed_set:
            raise DomainSerializationError(f"{field}_id_not_allowed:{identifier}")
    return normalized


def _payload_ids(payload: Mapping[str, Any], *keys: str) -> tuple[str, ...]:
    for key in keys:
        value = payload.get(key)
        if isinstance(value, list):
            return _deduplicate(str(item) for item in value)
    return ()


def _assessment_status(value: Any) -> str:
    text = str(value or "").casefold()
    if any(term in text for term in ("invalid", "non valido", "challeng", "contest")):
        return "challenged"
    return "confirmed"


def _critic_ids(payload: Mapping[str, Any], kind: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    confirmed_key = f"confirmed_{kind}_ids"
    challenged_key = f"challenged_{kind}_ids"
    if confirmed_key in payload or challenged_key in payload:
        return _payload_ids(payload, confirmed_key), _payload_ids(payload, challenged_key)
    id_key = "rule_id" if kind == "rule" else "source_id"
    assessments = payload.get(f"{kind}_assessments")
    confirmed: list[str] = []
    challenged: list[str] = []
    for item in assessments if isinstance(assessments, list) else []:
        if not isinstance(item, Mapping) or not item.get(id_key):
            continue
        target = challenged if _assessment_status(item.get("assessment")) == "challenged" else confirmed
        identifier = str(item[id_key])
        if identifier not in target:
            target.append(identifier)
    return tuple(confirmed), tuple(challenged)


@dataclass(frozen=True)
class EvidenceSlot:
    number: int
    kind: str
    evidence_id: str
    critic_status: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "number": self.number,
            "kind": self.kind,
            "evidence_id": self.evidence_id,
            "selection_origin": "planner",
            "critic_status": self.critic_status,
        }


@dataclass(frozen=True)
class EvidencePacket:
    protocol_version: str
    allowed_rule_ids: tuple[str, ...]
    allowed_source_ids: tuple[str, ...]
    planner_rule_ids: tuple[str, ...]
    planner_source_ids: tuple[str, ...]
    critic_confirmed_rule_ids: tuple[str, ...]
    critic_challenged_rule_ids: tuple[str, ...]
    critic_confirmed_source_ids: tuple[str, ...]
    critic_challenged_source_ids: tuple[str, ...]
    contradictions: tuple[Any, ...]

    def __post_init__(self) -> None:
        if self.protocol_version != PROTOCOL_VERSION:
            raise DomainSerializationError("evidence_packet_version_invalid")
        if DEMO_RULE_ID in self.allowed_rule_ids or DEMO_SOURCE_ID in self.allowed_source_ids:
            raise DomainSerializationError("demo_id_not_allowed")
        fields = (
            (self.planner_rule_ids, self.allowed_rule_ids, "planner_rule"),
            (self.planner_source_ids, self.allowed_source_ids, "planner_source"),
            (self.critic_confirmed_rule_ids, self.allowed_rule_ids, "critic_confirmed_rule"),
            (self.critic_challenged_rule_ids, self.allowed_rule_ids, "critic_challenged_rule"),
            (self.critic_confirmed_source_ids, self.allowed_source_ids, "critic_confirmed_source"),
            (self.critic_challenged_source_ids, self.allowed_source_ids, "critic_challenged_source"),
        )
        for values, allowed, field in fields:
            if tuple(values) != _deduplicate(values):
                raise DomainSerializationError(f"{field}_ids_not_deduplicated")
            _validate_ids(values, allowed, field)

    def _status(self, kind: str, identifier: str) -> str:
        challenged = self.critic_challenged_rule_ids if kind == "rule" else self.critic_challenged_source_ids
        confirmed = self.critic_confirmed_rule_ids if kind == "rule" else self.critic_confirmed_source_ids
        if identifier in challenged:
            return "challenged"
        if identifier in confirmed:
            return "confirmed"
        return "unreviewed"

    def slots(self) -> tuple[EvidenceSlot, ...]:
        entries = [
            *(('rule', identifier) for identifier in self.planner_rule_ids),
            *(('source', identifier) for identifier in self.planner_source_ids),
        ]
        return tuple(
            EvidenceSlot(index, kind, identifier, self._status(kind, identifier))
            for index, (kind, identifier) in enumerate(entries, start=1)
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "protocol_version": self.protocol_version,
            "allowed_rule_ids": list(self.allowed_rule_ids),
            "allowed_source_ids": list(self.allowed_source_ids),
            "planner_rule_ids": list(self.planner_rule_ids),
            "planner_source_ids": list(self.planner_source_ids),
            "critic_confirmed_rule_ids": list(self.critic_confirmed_rule_ids),
            "critic_challenged_rule_ids": list(self.critic_challenged_rule_ids),
            "critic_confirmed_source_ids": list(self.critic_confirmed_source_ids),
            "critic_challenged_source_ids": list(self.critic_challenged_source_ids),
            "contradictions": list(self.contradictions),
        }


def build_evidence_packet(
    *,
    allowed_rule_ids: Iterable[str],
    allowed_source_ids: Iterable[str],
    planner_output: Mapping[str, Any],
    critic_output: Mapping[str, Any],
) -> EvidencePacket:
    allowed_rules = _deduplicate(allowed_rule_ids)
    allowed_sources = _deduplicate(allowed_source_ids)
    planner_rules = _validate_ids(
        _payload_ids(planner_output, "rule_ids", "rules_selected", "selected_rule_ids"),
        allowed_rules,
        "planner_rule",
    )
    planner_sources = _validate_ids(
        _payload_ids(planner_output, "source_ids", "sources_selected", "selected_source_ids"),
        allowed_sources,
        "planner_source",
    )
    confirmed_rules, challenged_rules = _critic_ids(critic_output, "rule")
    confirmed_sources, challenged_sources = _critic_ids(critic_output, "source")
    contradictions = critic_output.get("contradictions")
    contradictions = tuple(contradictions) if isinstance(contradictions, list) else ()
    serialized_contradictions = json.dumps(contradictions, ensure_ascii=False)
    allowed_all = set(allowed_rules) | set(allowed_sources)
    for identifier in ID_MENTION_RE.findall(serialized_contradictions):
        if identifier not in allowed_all:
            raise DomainSerializationError(f"contradiction_id_not_allowed:{identifier}")
    if DEMO_RULE_ID in serialized_contradictions or DEMO_SOURCE_ID in serialized_contradictions:
        raise DomainSerializationError("demo_id_not_allowed")
    return EvidencePacket(
        protocol_version=PROTOCOL_VERSION,
        allowed_rule_ids=allowed_rules,
        allowed_source_ids=allowed_sources,
        planner_rule_ids=planner_rules,
        planner_source_ids=planner_sources,
        critic_confirmed_rule_ids=_validate_ids(confirmed_rules, allowed_rules, "critic_confirmed_rule"),
        critic_challenged_rule_ids=_validate_ids(challenged_rules, allowed_rules, "critic_challenged_rule"),
        critic_confirmed_source_ids=_validate_ids(confirmed_sources, allowed_sources, "critic_confirmed_source"),
        critic_challenged_source_ids=_validate_ids(challenged_sources, allowed_sources, "critic_challenged_source"),
        contradictions=contradictions,
    )


@dataclass(frozen=True)
class SolverArgument:
    text: str
    evidence_slots: tuple[int, ...] = ()


@dataclass(frozen=True)
class ProvenanceSolverRecord:
    position: str
    support: tuple[SolverArgument, ...]
    counter: tuple[SolverArgument, ...]
    uncertainty: tuple[str, ...]
    alternatives: tuple[str, ...]
    recommendation: str
    confidence: float
    human: bool


def _argument(value: Any, field: str, slot_count: int) -> SolverArgument:
    if isinstance(value, str):
        return SolverArgument(_bounded_text(value, field))
    if not isinstance(value, Mapping) or set(value) - {"text", "evidence_slots"} or "text" not in value:
        raise DomainSerializationError(f"{field}_argument_invalid")
    raw_slots = value.get("evidence_slots") or []
    if not isinstance(raw_slots, list):
        raise DomainSerializationError("evidence_slots_invalid")
    slots: list[int] = []
    for slot in raw_slots:
        if not isinstance(slot, int) or isinstance(slot, bool) or not 1 <= slot <= slot_count:
            raise DomainSerializationError(f"evidence_slot_out_of_range:{slot}")
        if slot not in slots:
            slots.append(slot)
    return SolverArgument(_bounded_text(value["text"], field), tuple(slots))


def parse_provenance_solver_json(raw: str, packet: EvidencePacket) -> ProvenanceSolverRecord:
    try:
        payload = json.loads(str(raw).strip())
    except json.JSONDecodeError as exc:
        raise DomainSerializationError("provenance_solver_json_invalid") from exc
    if not isinstance(payload, dict) or set(payload) != SOLVER_FIELDS:
        raise DomainSerializationError("provenance_solver_fields_invalid")
    if not isinstance(payload["support"], list) or not 1 <= len(payload["support"]) <= 3:
        raise DomainSerializationError("support_list_invalid")
    if not isinstance(payload["counter"], list) or not 1 <= len(payload["counter"]) <= 3:
        raise DomainSerializationError("counter_list_invalid")
    if not isinstance(payload["human"], bool):
        raise DomainSerializationError("human_type_invalid")
    confidence = payload["confidence"]
    if not isinstance(confidence, (int, float)) or isinstance(confidence, bool) or not 0 <= float(confidence) <= 1:
        raise DomainSerializationError("confidence_out_of_range")
    record = ProvenanceSolverRecord(
        position=_bounded_text(payload["position"], "position"),
        support=tuple(_argument(item, "support", len(packet.slots())) for item in payload["support"]),
        counter=tuple(_argument(item, "counter", len(packet.slots())) for item in payload["counter"]),
        uncertainty=_bounded_strings(payload["uncertainty"], "uncertainty", 3),
        alternatives=_bounded_strings(payload["alternatives"], "alternatives", 2),
        recommendation=_bounded_text(payload["recommendation"], "recommendation"),
        confidence=float(confidence),
        human=payload["human"],
    )
    serialized = json.dumps(payload, ensure_ascii=False)
    if detect_demo_contamination(serialized):
        raise DomainSerializationError("demo_id_not_allowed")
    allowed = set(packet.allowed_rule_ids) | set(packet.allowed_source_ids)
    for identifier in ID_MENTION_RE.findall(serialized):
        if identifier not in allowed:
            raise DomainSerializationError(f"solver_id_not_allowed:{identifier}")
    return record


def parse_provenance_canonical_record(raw: str, packet: EvidencePacket) -> ProvenanceSolverRecord:
    lines = [line.strip() for line in str(raw).splitlines() if line.strip()]
    if not lines or lines[-1] != "END":
        raise DomainSerializationError("provenance_canonical_end_missing")
    if len(lines) > 24:
        raise DomainSerializationError("provenance_canonical_line_limit_exceeded")
    values: dict[str, list[str]] = {name: [] for name in CANONICAL_ORDER}
    last_order = -1
    repeatable = {"SUPPORT", "COUNTER", "UNCERTAINTY", "ALTERNATIVE"}
    pending_label: str | None = None
    for line in lines[:-1]:
        if line.startswith("- "):
            if pending_label is None:
                raise DomainSerializationError("provenance_canonical_unknown_line")
            label, raw_value = pending_label, line[2:]
        else:
            if ":" not in line:
                raise DomainSerializationError("provenance_canonical_unknown_line")
            label, raw_value = line.split(":", 1)
            label = label.strip()
            if label not in CANONICAL_ORDER:
                raise DomainSerializationError("provenance_canonical_unknown_line")
            pending_label = label if not raw_value.strip() else None
        order = CANONICAL_ORDER[label]
        if order < last_order:
            raise DomainSerializationError("provenance_canonical_order_invalid")
        last_order = order
        if label not in repeatable and values[label]:
            raise DomainSerializationError(f"provenance_canonical_duplicate:{label.lower()}")
        text = raw_value.strip()
        if not text and pending_label == label:
            continue
        if not text:
            raise DomainSerializationError("solver_semantics_incomplete")
        if text:
            values[label].append(_bounded_text(text, label.lower()))
    required = {"POSITION", "SUPPORT", "COUNTER", "RECOMMENDATION", "CONFIDENCE", "HUMAN"}
    if any(not values[field] for field in required):
        raise DomainSerializationError("solver_semantics_incomplete")
    if any(len(values[field]) > limit for field, limit in (("SUPPORT", 3), ("COUNTER", 3), ("UNCERTAINTY", 3), ("ALTERNATIVE", 2))):
        raise DomainSerializationError("provenance_canonical_list_limit_exceeded")
    try:
        confidence = float(values["CONFIDENCE"][0])
    except ValueError as exc:
        raise DomainSerializationError("confidence_out_of_range") from exc
    if not 0 <= confidence <= 1:
        raise DomainSerializationError("confidence_out_of_range")
    human = values["HUMAN"][0].casefold()
    if human not in {"true", "false"}:
        raise DomainSerializationError("human_type_invalid")
    record = ProvenanceSolverRecord(
        position=values["POSITION"][0],
        support=tuple(SolverArgument(item) for item in values["SUPPORT"]),
        counter=tuple(SolverArgument(item) for item in values["COUNTER"]),
        uncertainty=tuple(values["UNCERTAINTY"]),
        alternatives=tuple(values["ALTERNATIVE"]),
        recommendation=values["RECOMMENDATION"][0],
        confidence=confidence,
        human=human == "true",
    )
    serialized = json.dumps(values, ensure_ascii=False)
    if detect_demo_contamination(serialized):
        raise DomainSerializationError("demo_id_not_allowed")
    allowed = set(packet.allowed_rule_ids) | set(packet.allowed_source_ids)
    for identifier in ID_MENTION_RE.findall(serialized):
        if identifier not in allowed:
            raise DomainSerializationError(f"solver_id_not_allowed:{identifier}")
    return record


def detect_demo_contamination(raw: str) -> bool:
    text = str(raw)
    return DEMO_RULE_ID in text or DEMO_SOURCE_ID in text


def _rule_application(packet: EvidencePacket) -> list[dict[str, str]]:
    output = []
    for identifier in packet.planner_rule_ids:
        status = packet._status("rule", identifier)
        if status == "confirmed":
            application = "selected_by_planner_and_reviewed_by_critic"
        elif status == "challenged":
            application = "selected_by_planner_and_challenged_by_critic"
        else:
            application = "selected_by_planner"
        output.append({"rule_id": identifier, "status": status, "application": application})
    return output


def serialize_with_evidence_packet(
    record: ProvenanceSolverRecord,
    context: Mapping[str, str],
    packet: EvidencePacket,
) -> dict[str, Any]:
    slots = packet.slots()

    def claims(arguments: tuple[SolverArgument, ...]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        public, metadata = [], []
        for index, argument in enumerate(arguments):
            references = [slots[slot - 1].evidence_id for slot in argument.evidence_slots]
            public.append({"text": argument.text, "kind": "inference", "refs": references})
            metadata.append(
                {
                    "argument_index": index,
                    "text": argument.text,
                    "evidence_slots": list(argument.evidence_slots),
                    "evidence_ids": references,
                }
            )
        return public, metadata

    support, support_meta = claims(record.support)
    counter, counter_meta = claims(record.counter)
    rule_application = _rule_application(packet)
    evidence_used = [{"kind": "source", "id": identifier} for identifier in packet.planner_source_ids]
    payload = {
        "domain_id": str(context["domain_id"]),
        "question": str(context["question"]),
        "position": record.position,
        "supporting_arguments": support,
        "counterarguments": counter,
        "rule_application": rule_application,
        "evidence_used": evidence_used,
        "uncertainties": list(record.uncertainty),
        "alternative_interpretations": list(record.alternatives),
        "recommendation": record.recommendation,
        "confidence": record.confidence,
        "human_decision_required": record.human,
    }
    request = DomainReasoningInput(
        domain_id=str(context["domain_id"]),
        domain_version="canary-v1",
        question=str(context["question"]),
        facts=(),
        rules=tuple({"rule_id": item} for item in packet.allowed_rule_ids),
        sources=tuple({"source_id": item} for item in packet.allowed_source_ids),
        constraints=(),
        known_contradictions=tuple(item for item in packet.contradictions if isinstance(item, dict)),
    )
    validation = validate_domain_opinion(payload, request)
    if not validation["ok"]:
        raise DomainSerializationError("final_schema_invalid:" + ",".join(validation["errors"]))
    selected = [
        *(slot.to_dict() for slot in slots if slot.kind == "rule"),
        *(slot.to_dict() for slot in slots if slot.kind == "source"),
    ]
    metadata = {
        "protocol_version": PROTOCOL_VERSION,
        "evidence_used": selected,
        "evidence_confirmed": [item for item in selected if item["critic_status"] == "confirmed"],
        "evidence_challenged": [item for item in selected if item["critic_status"] == "challenged"],
        "argument_provenance": {"support": support_meta, "counter": counter_meta},
        "argument_level_provenance_missing": any(
            not item["evidence_slots"] for item in [*support_meta, *counter_meta]
        ),
    }
    return {"payload": payload, "metadata": metadata, "validation": validation}


def build_provenance_solver_prompt(
    case: Mapping[str, Any],
    critic: Mapping[str, Any],
    packet: EvidencePacket,
    *,
    request_slots: bool,
    demo_mode: str = "none",
) -> str:
    if demo_mode not in DEMO_MODES:
        raise ValueError("unknown_demo_mode")
    rules = {str(item.get("rule_id")): str(item.get("statement") or "") for item in case.get("rules", [])}
    sources = {str(item.get("source_id")): str(item.get("statement") or "") for item in case.get("sources", [])}
    evidence_lines = []
    for slot in packet.slots():
        statement = rules.get(slot.evidence_id, "") if slot.kind == "rule" else sources.get(slot.evidence_id, "")
        evidence_lines.append(
            f"EVIDENCE {slot.number} = {slot.kind} {slot.evidence_id} [{slot.critic_status}] :: {statement}"
        )
    if request_slots:
        example_slots = "[1]" if packet.slots() else "[]"
        arguments = '[{"text":"string","evidence_slots":' + example_slots + "}]"
        slot_instruction = "Use only valid EVIDENCE slot numbers; slots are optional per argument."
        schema = (
            '{"position":"string","support":' + arguments + ',"counter":' + arguments
            + ',"uncertainty":["string"],"alternatives":["string"],'
            '"recommendation":"string","confidence":0.0,"human":false}'
        )
    else:
        slot_instruction = "Do not output provenance IDs or evidence slots; provenance is preserved upstream."
        schema = (
            "POSITION: <text>\nSUPPORT: <text>\nCOUNTER: <text>\nUNCERTAINTY: <text>\n"
            "ALTERNATIVE: <text>\nRECOMMENDATION: <text>\nCONFIDENCE: 0.0\nHUMAN: false\nEND"
        )
    sections = [
        "ROLE: Domain decision solver. Produce an opinion, not provenance reconstruction or approval.",
        "QUESTION: " + str(case["question"]),
        "FACTS: " + json.dumps(case.get("facts") or [], ensure_ascii=False, separators=(",", ":")),
        "IMMUTABLE EVIDENCE PACKET:\n" + ("\n".join(evidence_lines) if evidence_lines else "NO UPSTREAM EVIDENCE SELECTED"),
        "DOMAIN OBJECTION: " + str(case.get("objection") or "Identify the strongest reasonable objection from supplied facts."),
        "KNOWN DOMAIN CONTRADICTIONS: "
        + json.dumps(case.get("known_contradictions") or [], ensure_ascii=False, separators=(",", ":")),
        "CONTRADICTIONS: " + json.dumps(packet.contradictions, ensure_ascii=False, separators=(",", ":")),
        "CRITIC: " + json.dumps(
            {
                "strongest_counterargument": critic.get("strongest_counterargument") or "",
                "unresolved_issues": critic.get("unresolved_issues") or [],
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ),
        "SEMANTICS: Answer the current question with position, support, strongest counterargument, uncertainty, alternative, conditional recommendation when needed, confidence, and human-review need.",
        "REQUIRED: support, counter, and uncertainty must each be nonempty; alternatives must be a list. Never emit an empty counter. Position and recommendation must each be one sentence; every string <=160 characters.",
        slot_instruction,
    ]
    if demo_mode == "skeleton":
        sections.append("STRUCTURE SKELETON ONLY; angle-bracket values are not an answer: " + schema.replace('"string"', '"<VALUE>"'))
    elif demo_mode == "invalid_ids":
        sections.append(
            "STRUCTURE-ONLY INVALID PLACEHOLDERS: "
            + DEMO_RULE_ID
            + " and "
            + DEMO_SOURCE_ID
            + " are never valid and must never appear in output."
        )
    if request_slots:
        sections.append("OUTPUT: JSON only, no markdown or surrounding text: " + schema)
    else:
        sections.append(
            "OUTPUT: Fixed record only; exact label order; repeat only SUPPORT, COUNTER, UNCERTAINTY, ALTERNATIVE; END mandatory:\n"
            + schema
        )
    sections.append("LIMITS: support/counter<=3; uncertainty<=3; alternatives<=2; every string<=160 characters.")
    return "\n".join(sections)


def structured_provenance_gate(metrics: Mapping[str, Any]) -> dict[str, Any]:
    passed = bool(
        int(metrics.get("semantic_complete_count") or 0) >= 7
        and int(metrics.get("final_schema_valid_count") or 0) == 8
        and float(metrics.get("final_rule_accuracy") or 0) >= 0.875
        and float(metrics.get("final_source_accuracy") or 0) >= 0.875
        and int(metrics.get("invented_id_count") or 0) == 0
        and int(metrics.get("demo_contamination_count") or 0) == 0
        and float(metrics.get("contradiction_inclusion") or 0) == 1.0
        and float(metrics.get("counterargument_coverage") or 0) == 1.0
        and int(metrics.get("safety_violations") or 0) == 0
    )
    return {
        "passed": passed,
        "classification": (
            "instruct_base_sufficient_with_structured_provenance"
            if passed
            else "provenance_pipeline_insufficient"
        ),
    }
