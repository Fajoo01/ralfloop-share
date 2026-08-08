from __future__ import annotations

from dataclasses import dataclass
import json
import re
from typing import Any, Iterable, Mapping

from .domain_opinion import DomainReasoningInput, validate_domain_opinion


MAX_TEXT_LENGTH = 240
DEMO_RULE_ID = "RULE_DEMO_NEVER_VALID"
DEMO_SOURCE_ID = "SOURCE_DEMO_NEVER_VALID"
COMPACT_FIELDS = {
    "position",
    "support",
    "counter",
    "rules",
    "sources",
    "uncertainty",
    "alternatives",
    "recommendation",
    "confidence",
    "human",
}
LIST_LIMITS = {
    "support": 3,
    "counter": 3,
    "rules": 5,
    "sources": 5,
    "uncertainty": 3,
    "alternatives": 2,
}
CANONICAL_ORDER = {
    "POSITION": 0,
    "SUPPORT": 1,
    "COUNTER": 2,
    "RULES": 3,
    "SOURCES": 4,
    "UNCERTAINTY": 5,
    "ALTERNATIVE": 6,
    "RECOMMENDATION": 7,
    "CONFIDENCE": 8,
    "HUMAN": 9,
}

ID_MENTION_RE = re.compile(
    r"(?<![A-Za-z0-9_])(?:[RS]_[A-Za-z0-9_.:-]*[A-Za-z0-9_]|[RS]X?\d(?:[A-Za-z0-9_.:-]*[A-Za-z0-9_])?)(?![A-Za-z0-9_])"
)


class DomainSerializationError(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class SolverSemanticRecord:
    position: str
    support: tuple[str, ...]
    counter: tuple[str, ...]
    rules: tuple[str, ...]
    sources: tuple[str, ...]
    uncertainty: tuple[str, ...]
    alternatives: tuple[str, ...]
    recommendation: str
    confidence: float
    human: bool


@dataclass(frozen=True)
class SerializationContext:
    domain_id: str
    question: str
    allowed_rule_ids: tuple[str, ...]
    allowed_source_ids: tuple[str, ...]


def _text(value: Any, field: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise DomainSerializationError(f"{field}_type_invalid")
    normalized = value.strip()
    if not normalized and not allow_empty:
        raise DomainSerializationError("solver_semantics_incomplete")
    if len(normalized) > MAX_TEXT_LENGTH:
        raise DomainSerializationError(f"{field}_too_long")
    return normalized


def _strings(value: Any, field: str) -> tuple[str, ...]:
    if not isinstance(value, list) or len(value) > LIST_LIMITS[field]:
        raise DomainSerializationError(f"{field}_list_invalid")
    return tuple(_text(item, field) for item in value)


def _confidence(value: Any) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool) or not 0.0 <= float(value) <= 1.0:
        raise DomainSerializationError("confidence_out_of_range")
    return float(value)


def _ensure_essential(record: SolverSemanticRecord) -> None:
    if not record.position or not record.support or not record.counter or not record.recommendation:
        raise DomainSerializationError("solver_semantics_incomplete")


def parse_compact_json(raw: str) -> SolverSemanticRecord:
    try:
        payload = json.loads(str(raw).strip())
    except json.JSONDecodeError as exc:
        raise DomainSerializationError("compact_json_invalid") from exc
    if not isinstance(payload, dict):
        raise DomainSerializationError("compact_json_not_object")
    if set(payload) - COMPACT_FIELDS:
        raise DomainSerializationError("compact_json_unknown_field")
    if COMPACT_FIELDS - set(payload):
        raise DomainSerializationError("solver_semantics_incomplete")
    if not isinstance(payload["human"], bool):
        raise DomainSerializationError("human_type_invalid")
    record = SolverSemanticRecord(
        position=_text(payload["position"], "position"),
        support=_strings(payload["support"], "support"),
        counter=_strings(payload["counter"], "counter"),
        rules=_strings(payload["rules"], "rules"),
        sources=_strings(payload["sources"], "sources"),
        uncertainty=_strings(payload["uncertainty"], "uncertainty"),
        alternatives=_strings(payload["alternatives"], "alternatives"),
        recommendation=_text(payload["recommendation"], "recommendation"),
        confidence=_confidence(payload["confidence"]),
        human=payload["human"],
    )
    _ensure_essential(record)
    return record


def parse_canonical_record(raw: str) -> SolverSemanticRecord:
    lines = [line.strip() for line in str(raw).splitlines() if line.strip()]
    if not lines or lines[-1] != "END":
        raise DomainSerializationError("canonical_end_missing")
    if len(lines) > 24:
        raise DomainSerializationError("canonical_line_limit_exceeded")
    values: dict[str, list[str]] = {name: [] for name in CANONICAL_ORDER}
    last_order = -1
    for line in lines[:-1]:
        if ":" not in line:
            raise DomainSerializationError("canonical_unknown_line")
        label, raw_value = line.split(":", 1)
        label = label.strip()
        if label not in CANONICAL_ORDER:
            raise DomainSerializationError("canonical_unknown_line")
        order = CANONICAL_ORDER[label]
        if order < last_order:
            raise DomainSerializationError("canonical_order_invalid")
        last_order = order
        if label not in {"SUPPORT", "COUNTER", "UNCERTAINTY", "ALTERNATIVE"} and values[label]:
            raise DomainSerializationError(f"canonical_duplicate:{label.lower()}")
        value = _text(raw_value, label.lower(), allow_empty=label in {"RULES", "SOURCES", "UNCERTAINTY", "ALTERNATIVE"})
        if value:
            values[label].append(value)
    required = {"POSITION", "SUPPORT", "COUNTER", "RULES", "SOURCES", "RECOMMENDATION", "CONFIDENCE", "HUMAN"}
    if any(not values[name] for name in required - {"RULES", "SOURCES"}):
        raise DomainSerializationError("solver_semantics_incomplete")
    if len(values["SUPPORT"]) > 3 or len(values["COUNTER"]) > 3 or len(values["UNCERTAINTY"]) > 3 or len(values["ALTERNATIVE"]) > 2:
        raise DomainSerializationError("canonical_list_limit_exceeded")
    rules = tuple(item.strip() for item in (values["RULES"][0].split(",") if values["RULES"] else []) if item.strip())
    sources = tuple(item.strip() for item in (values["SOURCES"][0].split(",") if values["SOURCES"] else []) if item.strip())
    if len(rules) > 5 or len(sources) > 5:
        raise DomainSerializationError("canonical_id_limit_exceeded")
    human_text = values["HUMAN"][0].lower()
    if human_text not in {"true", "false"}:
        raise DomainSerializationError("human_type_invalid")
    try:
        confidence: Any = float(values["CONFIDENCE"][0])
    except ValueError as exc:
        raise DomainSerializationError("confidence_out_of_range") from exc
    record = SolverSemanticRecord(
        position=values["POSITION"][0],
        support=tuple(values["SUPPORT"]),
        counter=tuple(values["COUNTER"]),
        rules=rules,
        sources=sources,
        uncertainty=tuple(values["UNCERTAINTY"]),
        alternatives=tuple(values["ALTERNATIVE"]),
        recommendation=values["RECOMMENDATION"][0],
        confidence=_confidence(confidence),
        human=human_text == "true",
    )
    _ensure_essential(record)
    return record


def _deduplicate_allowed(values: Iterable[str], allowed: tuple[str, ...], kind: str) -> list[str]:
    allowed_set = set(allowed)
    output: list[str] = []
    for value in values:
        if value not in allowed_set:
            raise DomainSerializationError(f"{kind}_id_not_allowed:{value}")
        if value not in output:
            output.append(value)
    return output


def _reject_unallowed_id_mentions(record: SolverSemanticRecord, context: SerializationContext) -> None:
    allowed = set(context.allowed_rule_ids) | set(context.allowed_source_ids)
    text_values = (
        record.position,
        *record.support,
        *record.counter,
        *record.uncertainty,
        *record.alternatives,
        record.recommendation,
    )
    for value in (*text_values, *record.rules, *record.sources):
        if DEMO_RULE_ID in value or DEMO_SOURCE_ID in value:
            raise DomainSerializationError("demo_id_not_allowed")
        for identifier in ID_MENTION_RE.findall(value):
            if identifier in allowed:
                continue
            kind = "source" if identifier.startswith("S") else "rule"
            raise DomainSerializationError(f"{kind}_id_not_allowed:{identifier}")


def serialize_domain_opinion(record: SolverSemanticRecord, context: SerializationContext) -> dict[str, Any]:
    _ensure_essential(record)
    _reject_unallowed_id_mentions(record, context)
    rule_ids = _deduplicate_allowed(record.rules, context.allowed_rule_ids, "rule")
    source_ids = _deduplicate_allowed(record.sources, context.allowed_source_ids, "source")
    payload = {
        "domain_id": context.domain_id,
        "question": context.question,
        "position": record.position,
        "supporting_arguments": [{"text": item, "kind": "inference", "refs": []} for item in record.support],
        "counterarguments": [{"text": item, "kind": "inference", "refs": []} for item in record.counter],
        "rule_application": [{"rule_id": item, "application": "selected_by_solver"} for item in rule_ids],
        "evidence_used": [{"kind": "source", "id": item} for item in source_ids],
        "uncertainties": list(record.uncertainty),
        "alternative_interpretations": list(record.alternatives),
        "recommendation": record.recommendation,
        "confidence": record.confidence,
        "human_decision_required": record.human,
    }
    request = DomainReasoningInput(
        domain_id=context.domain_id,
        domain_version="canary-v1",
        question=context.question,
        facts=(),
        rules=tuple({"rule_id": item} for item in context.allowed_rule_ids),
        sources=tuple({"source_id": item} for item in context.allowed_source_ids),
        constraints=(),
        known_contradictions=(),
    )
    validation = validate_domain_opinion(payload, request)
    if not validation["ok"]:
        raise DomainSerializationError("final_schema_invalid:" + ",".join(validation["errors"]))
    return payload


def detect_truncation(raw: str, token_count: int, max_new_tokens: int, eos_seen: bool) -> dict[str, Any]:
    truncated = token_count >= max_new_tokens and not eos_seen
    labels = [
        "position",
        "support",
        "counter",
        "rules",
        "sources",
        "uncertainty",
        "alternatives",
        "recommendation",
        "confidence",
        "human",
    ]
    positions = [(str(raw).rfind(f'"{name}"'), name) for name in labels if str(raw).rfind(f'"{name}"') >= 0]
    return {
        "truncated": truncated,
        "field": max(positions)[1] if truncated and positions else None,
        "content_recoverable": str(raw).lstrip().startswith(("{", "POSITION:")),
    }


def semantic_complete(record: SolverSemanticRecord, *, uncertainty_required: bool) -> bool:
    return bool(
        record.position
        and record.support
        and record.counter
        and record.recommendation
        and 0.0 <= record.confidence <= 1.0
        and (record.uncertainty or not uncertainty_required)
    )


def reference_accuracy(selected: Iterable[str], required: Iterable[str], allowed: Iterable[str]) -> float:
    selected_set = set(selected)
    required_set = set(required)
    if selected_set - set(allowed):
        return 0.0
    if not required_set:
        return 1.0
    return len(selected_set & required_set) / len(required_set)


def build_solver_semantic_prompt(
    case: Mapping[str, Any],
    critic: Mapping[str, Any],
    *,
    contract: str,
    one_shot: bool = False,
) -> str:
    if contract not in {"B", "C"}:
        raise ValueError("unknown_solver_semantic_contract")
    facts = [{"id": item.get("fact_id"), "text": item.get("statement")} for item in case["facts"]]
    rules = [{"id": item.get("rule_id"), "text": item.get("statement")} for item in case["rules"]]
    sources = [{"id": item.get("source_id"), "text": item.get("statement")} for item in case["sources"]]
    critique = {
        "contradictions": critic.get("contradictions") or [],
        "strongest_counterargument": critic.get("strongest_counterargument") or "",
        "unresolved_issues": critic.get("unresolved_issues") or [],
    }
    sections = [
        "ROLE: Domain decision solver. No approval or external action.",
        "QUESTION: " + str(case["question"]),
        "FACTS: " + json.dumps(facts, ensure_ascii=False, separators=(",", ":")),
        "RULES: " + json.dumps(rules, ensure_ascii=False, separators=(",", ":")),
        "SOURCES: " + json.dumps(sources, ensure_ascii=False, separators=(",", ":")),
        "CRITIC: " + json.dumps(critique, ensure_ascii=False, separators=(",", ":")),
        "SEMANTICS: Answer only the current question; include position, support, strongest counterargument, uncertainty, alternative, conditional recommendation when facts are insufficient, confidence, and human-review need. Never mention an object absent from the current case or output an approval.",
        "SELECTION: RULES and SOURCES lines are mandatory and nonempty. Copy exact current-case IDs used by the decision. Include the threshold rule/source; include the incompleteness rule and conflicting sources when applicable. Exclude graphical/irrelevant entries. Never invent or copy demo IDs.",
    ]
    if one_shot:
        if contract == "B":
            sections.insert(1,
                'STRUCTURE-ONLY EXAMPLE; placeholders are not semantic content and demo IDs are always invalid. OUTPUT: {"position":"<POSITION>","support":["<SUPPORT>"],"counter":["<COUNTER>"],"rules":["RULE_DEMO_NEVER_VALID"],"sources":["SOURCE_DEMO_NEVER_VALID"],"uncertainty":["<UNCERTAINTY>"],"alternatives":["<ALTERNATIVE>"],"recommendation":"<RECOMMENDATION>","confidence":0.0,"human":false}'
            )
        else:
            sections.insert(1,
                "STRUCTURE-ONLY EXAMPLE; placeholders are not semantic content and demo IDs are always invalid.\nOUTPUT:\nPOSITION: <POSITION>\nSUPPORT: <SUPPORT>\nCOUNTER: <COUNTER>\nRULES: RULE_DEMO_NEVER_VALID\nSOURCES: SOURCE_DEMO_NEVER_VALID\nUNCERTAINTY: <UNCERTAINTY>\nALTERNATIVE: <ALTERNATIVE>\nRECOMMENDATION: <RECOMMENDATION>\nCONFIDENCE: 0.0\nHUMAN: false\nEND"
            )
        sections.insert(2, "CURRENT CASE: Use only the following case; demo IDs are forbidden and validator-rejected.")
    if contract == "B":
        sections.extend(
            [
                'OUTPUT: JSON only, no markdown or surrounding text: {"position":"string","support":["string"],"counter":["string"],"rules":["RULE_ID"],"sources":["SOURCE_ID"],"uncertainty":["string"],"alternatives":["string"],"recommendation":"string","confidence":0.0,"human":false}',
                "LIMITS: support<=3; counter<=3; rules<=5; sources<=5; uncertainty<=3; alternatives<=2; each string<=240 characters.",
            ]
        )
    else:
        sections.extend(
            [
                "OUTPUT: Fixed record only. Every label is mandatory and must remain in this order: POSITION, SUPPORT, COUNTER, RULES, SOURCES, UNCERTAINTY, ALTERNATIVE, RECOMMENDATION, CONFIDENCE, HUMAN, END. Repeat only SUPPORT, COUNTER, UNCERTAINTY, ALTERNATIVE.",
                "LIMITS: one item per line; RULES/SOURCES comma-separated; <=24 lines; each value<=240 characters; END mandatory; no unknown line.",
            ]
        )
    return "\n".join(sections)
