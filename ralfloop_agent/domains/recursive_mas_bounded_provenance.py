from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import re
from typing import Any, Iterable, Mapping, Sequence

from .recursive_mas_provenance_selector_diagnostics import EvidenceSelection, deduplicate


DECISIONS = frozenset({"KEEP", "DROP", "UNCERTAIN"})
BINARY_DECISIONS = frozenset({"KEEP", "DROP"})
SLOT_RE = re.compile(r"^(RULE|SOURCE)_SLOT_(\d{2})$")
ASSIGNMENT_RE = re.compile(r"^(\d+)=([A-Z]+)$")


@dataclass(frozen=True)
class SlotMapping:
    rule_slots: tuple[tuple[str, str], ...]
    source_slots: tuple[tuple[str, str], ...]

    @classmethod
    def from_case(cls, case: Mapping[str, Any]) -> "SlotMapping":
        return cls(
            tuple((f"RULE_SLOT_{index:02d}", str(item["rule_id"])) for index, item in enumerate(case["rules"], 1)),
            tuple((f"SOURCE_SLOT_{index:02d}", str(item["source_id"])) for index, item in enumerate(case["sources"], 1)),
        )

    def to_dict(self) -> dict[str, str]:
        return dict((*self.rule_slots, *self.source_slots))

    def slot_for_id(self, identifier: str) -> str:
        for slot, real_id in (*self.rule_slots, *self.source_slots):
            if real_id == identifier:
                return slot
        raise ValueError("evidence_id_not_mapped")

    def real_id(self, slot: str) -> str:
        match = SLOT_RE.fullmatch(str(slot))
        if not match:
            raise ValueError("slot_invalid")
        mapping = self.to_dict()
        if slot not in mapping:
            raise ValueError("slot_out_of_range")
        return mapping[slot]

    def selection(self, rule_decisions: Sequence[str], source_decisions: Sequence[str]) -> EvidenceSelection:
        if len(rule_decisions) != len(self.rule_slots) or len(source_decisions) != len(self.source_slots):
            raise ValueError("slot_decision_count_invalid")
        if any(value not in BINARY_DECISIONS for value in (*rule_decisions, *source_decisions)):
            raise ValueError("unresolved_slot_decision")
        rules = tuple(real_id for (_, real_id), decision in zip(self.rule_slots, rule_decisions, strict=True) if decision == "KEEP")
        sources = tuple(real_id for (_, real_id), decision in zip(self.source_slots, source_decisions, strict=True) if decision == "KEEP")
        return EvidenceSelection(
            tuple(real_id for _, real_id in self.rule_slots),
            tuple(real_id for _, real_id in self.source_slots),
            rules,
            sources,
        )


@dataclass(frozen=True)
class BoundedDecisions:
    rules: tuple[str, ...]
    sources: tuple[str, ...]

    def __post_init__(self) -> None:
        if any(value not in DECISIONS for value in (*self.rules, *self.sources)):
            raise ValueError("bounded_decision_invalid")


def _parse_assignments(value: str, expected: int) -> tuple[str, ...]:
    parts = [item.strip() for item in value.split(",") if item.strip()]
    if len(parts) != expected:
        raise ValueError("bounded_assignment_count_invalid")
    decisions: list[str] = []
    for expected_index, part in enumerate(parts, 1):
        match = ASSIGNMENT_RE.fullmatch(part)
        if not match or int(match.group(1)) != expected_index:
            raise ValueError("bounded_slot_index_invalid")
        decision = match.group(2)
        if decision not in DECISIONS:
            raise ValueError("bounded_decision_invalid")
        decisions.append(decision)
    return tuple(decisions)


def parse_batch_bounded(raw: str, *, rule_count: int, source_count: int) -> BoundedDecisions:
    lines = [line.strip() for line in str(raw).replace("\r\n", "\n").splitlines() if line.strip()]
    if len(lines) != 3 or lines[-1] != "END" or not lines[0].startswith("R: ") or not lines[1].startswith("S: "):
        raise ValueError("bounded_batch_format_invalid")
    return BoundedDecisions(
        _parse_assignments(lines[0][3:], rule_count),
        _parse_assignments(lines[1][3:], source_count),
    )


def parse_itemwise_bounded(raw: str, *, binary: bool = False) -> str:
    lines = [line.strip() for line in str(raw).replace("\r\n", "\n").splitlines() if line.strip()]
    allowed = BINARY_DECISIONS if binary else DECISIONS
    if len(lines) != 2 or lines[1] != "END" or lines[0] not in allowed:
        raise ValueError("bounded_itemwise_format_invalid")
    return lines[0]


def apply_uncertain_policy(
    decisions: Sequence[str], policy: str, *, second_pass: Sequence[str] | None = None
) -> tuple[str, ...]:
    if policy == "P1_KEEP":
        return tuple("KEEP" if value == "UNCERTAIN" else value for value in decisions)
    if policy != "P2_SECOND_PASS":
        raise ValueError("uncertain_policy_invalid")
    resolutions = iter(second_pass or ())
    output: list[str] = []
    for value in decisions:
        if value != "UNCERTAIN":
            output.append(value)
            continue
        try:
            resolution = next(resolutions)
        except StopIteration as exc:
            raise ValueError("uncertain_resolution_missing") from exc
        if resolution not in BINARY_DECISIONS:
            raise ValueError("uncertain_resolution_invalid")
        output.append(resolution)
    try:
        next(resolutions)
    except StopIteration:
        return tuple(output)
    raise ValueError("uncertain_resolution_extra")


def selected_from_decisions(
    mapping: SlotMapping,
    decisions: BoundedDecisions,
    *,
    rule_policy: str,
    source_policy: str,
    rule_second_pass: Sequence[str] | None = None,
    source_second_pass: Sequence[str] | None = None,
) -> EvidenceSelection:
    rules = apply_uncertain_policy(decisions.rules, rule_policy, second_pass=rule_second_pass)
    sources = apply_uncertain_policy(decisions.sources, source_policy, second_pass=source_second_pass)
    return mapping.selection(rules, sources)


def batch_prompt(case: Mapping[str, Any], mapping: SlotMapping, *, rule_order: Sequence[str] | None = None, source_order: Sequence[str] | None = None) -> str:
    rule_by_id = {str(item["rule_id"]): str(item.get("statement") or "") for item in case["rules"]}
    source_by_id = {str(item["source_id"]): str(item.get("statement") or "") for item in case["sources"]}
    rule_slots = list(mapping.rule_slots)
    source_slots = list(mapping.source_slots)
    if rule_order is not None:
        positions = {identifier: index for index, identifier in enumerate(rule_order)}
        rule_slots.sort(key=lambda item: positions[item[1]])
    if source_order is not None:
        positions = {identifier: index for index, identifier in enumerate(source_order)}
        source_slots.sort(key=lambda item: positions[item[1]])
    rules = "\n".join(f"{slot}: {rule_by_id[real_id]}" for slot, real_id in rule_slots)
    sources = "\n".join(f"{slot}: {source_by_id[real_id]}" for slot, real_id in source_slots)
    r_schema = ",".join(f"{index}=DECISION" for index in range(1, len(mapping.rule_slots) + 1))
    s_schema = ",".join(f"{index}=DECISION" for index in range(1, len(mapping.source_slots) + 1))
    facts = json.dumps([str(item.get("statement") or "") for item in case.get("facts") or []], ensure_ascii=False, separators=(",", ":"))
    return "\n".join((
        "ROLE: Classify evidence relevance. Do not answer the question.",
        "QUESTION:\n" + str(case["question"]),
        "FACTS:\n" + facts,
        "RULE CANDIDATES:\n" + rules,
        "SOURCE CANDIDATES:\n" + sources,
        "KEEP only when removing evidence could change position, counterargument, uncertainty, recommendation, or provenance. DROP when removal cannot change any of them, even if evidence is true or topically similar. UNCERTAIN only when this test is unresolved.",
        "Contrary sources may both be KEEP.",
        "Replace every DECISION with exactly one of KEEP, DROP, UNCERTAIN. Output exactly three lines, no explanation, no identifiers:",
        "R: " + r_schema,
        "S: " + s_schema,
        "END",
    ))


def itemwise_prompt(case: Mapping[str, Any], *, candidate_type: str, slot: str, text: str, fact_texts: Sequence[str] | None = None, binary: bool = False) -> str:
    if candidate_type not in {"RULE", "SOURCE"} or not SLOT_RE.fullmatch(slot):
        raise ValueError("itemwise_candidate_invalid")
    facts = list(fact_texts) if fact_texts is not None else [str(item.get("statement") or "") for item in case.get("facts") or []]
    choices = "KEEP or DROP" if binary else "KEEP, DROP, or UNCERTAIN"
    type_rule = (
        "For RULE: KEEP only when rule changes eligibility, threshold, condition, required review, or handling of conflicting/incomplete evidence. DROP formatting, naming, or administrative rules with no substantive effect."
        if candidate_type == "RULE"
        else "For SOURCE: KEEP when source supplies an observation, constraint, uncertainty, or conflicting evidence that can affect decision or confidence. DROP administrative, decorative, or non-evaluative sources. Opposition to main recommendation is not a reason to DROP."
    )
    return "\n".join((
        "QUESTION:\n" + str(case["question"]),
        "FACTS:\n" + json.dumps(facts, ensure_ascii=False, separators=(",", ":")),
        "CANDIDATE TYPE:\n" + candidate_type,
        "CANDIDATE SLOT:\n" + slot,
        "CANDIDATE TEXT:\n" + str(text),
        "KEEP only if removing this candidate could change position, counterargument, uncertainty, recommendation, or provenance. DROP if removal cannot change any, even when candidate is true or topically similar. Contrary, conditional, and decision-limiting evidence may be KEEP.",
        type_rule,
        "Output exactly two lines. First line: one of " + choices + ". Second line: END. No explanation.",
    ))


def most_relevant_fact(case: Mapping[str, Any], candidate_text: str) -> str:
    tokens = set(re.findall(r"[\wÀ-ÿ]+", candidate_text.casefold()))
    facts = [str(item.get("statement") or "") for item in case.get("facts") or []]
    return max(facts, key=lambda value: (len(tokens & set(re.findall(r"[\wÀ-ÿ]+", value.casefold()))), -facts.index(value)), default="")


def decision_agreement(left: Mapping[str, str], right: Mapping[str, str]) -> float:
    keys = set(left) | set(right)
    return sum(left.get(key) == right.get(key) for key in keys) / len(keys) if keys else 1.0


def selection_jaccard(left: EvidenceSelection, right: EvidenceSelection) -> float:
    one = set(left.selected_rule_ids) | set(left.selected_source_ids)
    two = set(right.selected_rule_ids) | set(right.selected_source_ids)
    return len(one ^ two) / max(1, len(one | two))


def stable_sha256(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def verify_frozen_components(manifest: Mapping[str, Any], current_hashes: Mapping[str, str]) -> None:
    if not manifest.get("frozen"):
        raise RuntimeError("bounded_selector_not_frozen")
    for key, actual in current_hashes.items():
        if manifest.get(key) != actual:
            raise RuntimeError(f"bounded_selector_freeze_mismatch:{key}")
