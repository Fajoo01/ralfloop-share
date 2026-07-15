from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
import hashlib
import json
import math
from pathlib import Path
import re
import statistics
from typing import Any, Iterable, Mapping, Sequence

SELECTOR_IDS = ("selected_rule_ids", "selected_source_ids")
TOKEN_RE = re.compile(r"[\wÀ-ÿ]+", re.UNICODE)
STOPWORDS = {"a", "ad", "al", "alla", "alle", "anche", "che", "con", "da", "dal", "dalla", "dei", "del", "della", "di", "e", "gli", "i", "il", "in", "la", "le", "lo", "non", "o", "per", "più", "quale", "se", "su", "tra", "un", "una", "uno"}
CONDITIONAL = {"se", "quando", "salvo", "purché", "richiede", "condizione", "manca", "altrimenti"}
NEGATIONS = {"non", "nessun", "senza", "vietato", "incompatibile", "contraddittorio"}


def deduplicate(values: Iterable[Any]) -> tuple[str, ...]:
    output: list[str] = []
    for value in values:
        item = str(value).strip()
        if item and item not in output:
            output.append(item)
    return tuple(output)


@dataclass(frozen=True)
class EvidenceSelection:
    allowed_rule_ids: tuple[str, ...]
    allowed_source_ids: tuple[str, ...]
    selected_rule_ids: tuple[str, ...]
    selected_source_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        if set(self.selected_rule_ids) - set(self.allowed_rule_ids):
            raise ValueError("selected_rule_id_not_allowed")
        if set(self.selected_source_ids) - set(self.allowed_source_ids):
            raise ValueError("selected_source_id_not_allowed")
        if self.selected_rule_ids != deduplicate(self.selected_rule_ids):
            raise ValueError("selected_rule_ids_not_deduplicated")
        if self.selected_source_ids != deduplicate(self.selected_source_ids):
            raise ValueError("selected_source_ids_not_deduplicated")

    def to_dict(self) -> dict[str, list[str]]:
        return {"allowed_rule_ids": list(self.allowed_rule_ids), "allowed_source_ids": list(self.allowed_source_ids), "selected_rule_ids": list(self.selected_rule_ids), "selected_source_ids": list(self.selected_source_ids)}


def selection_from_payload(case: Mapping[str, Any], payload: Mapping[str, Any]) -> EvidenceSelection:
    allowed_rules = tuple(str(item["rule_id"]) for item in case["rules"])
    allowed_sources = tuple(str(item["source_id"]) for item in case["sources"])
    if set(payload) != set(SELECTOR_IDS) or any(not isinstance(payload[key], list) for key in SELECTOR_IDS):
        raise ValueError("selector_schema_invalid")
    return EvidenceSelection(allowed_rules, allowed_sources, deduplicate(payload["selected_rule_ids"]), deduplicate(payload["selected_source_ids"]))


def planner_selection(case: Mapping[str, Any], planner: Mapping[str, Any]) -> EvidenceSelection:
    return selection_from_payload(case, {"selected_rule_ids": planner.get("rules_selected") or [], "selected_source_ids": planner.get("sources_selected") or []})


def combine_selections(left: EvidenceSelection, right: EvidenceSelection, operation: str) -> EvidenceSelection:
    if (left.allowed_rule_ids, left.allowed_source_ids) != (right.allowed_rule_ids, right.allowed_source_ids):
        raise ValueError("selection_allowed_sets_differ")
    if operation == "union":
        rules = deduplicate((*left.selected_rule_ids, *right.selected_rule_ids))
        sources = deduplicate((*left.selected_source_ids, *right.selected_source_ids))
    elif operation == "intersection":
        rules = tuple(item for item in left.selected_rule_ids if item in set(right.selected_rule_ids))
        sources = tuple(item for item in left.selected_source_ids if item in set(right.selected_source_ids))
    else:
        raise ValueError("selection_operation_invalid")
    return EvidenceSelection(left.allowed_rule_ids, left.allowed_source_ids, rules, sources)


def _tokens(value: str) -> set[str]:
    return {token.casefold() for token in TOKEN_RE.findall(str(value)) if len(token) > 1 and token.casefold() not in STOPWORDS}


def deterministic_candidates(case: Mapping[str, Any], *, max_fraction: float = 0.75) -> EvidenceSelection:
    context = " ".join([str(case.get("question") or ""), *(str(item.get("statement") or "") for item in case.get("facts") or []), *(str(item) for item in case.get("constraints") or [])])
    context_tokens = _tokens(context)
    context_conditional = bool(context_tokens & CONDITIONAL)
    context_negative = bool(context_tokens & NEGATIONS)

    def select(rows: Sequence[Mapping[str, Any]], key: str) -> tuple[str, ...]:
        ranked = []
        for index, row in enumerate(rows):
            statement_tokens = _tokens(str(row.get("statement") or ""))
            overlap = len(context_tokens & statement_tokens)
            conditional = int(context_conditional and bool(statement_tokens & CONDITIONAL))
            negative = int(context_negative and bool(statement_tokens & NEGATIONS))
            entity = len({token for token in statement_tokens if len(token) >= 6} & context_tokens)
            ranked.append(((overlap * 4) + (entity * 2) + conditional + negative, -index, str(row[key])))
        limit = max(1, min(len(ranked), math.ceil(len(ranked) * max_fraction)))
        ranked.sort(reverse=True)
        return tuple(item[2] for item in ranked[:limit])

    rules, sources = tuple(case.get("rules") or []), tuple(case.get("sources") or [])
    return EvidenceSelection(tuple(str(item["rule_id"]) for item in rules), tuple(str(item["source_id"]) for item in sources), select(rules, "rule_id"), select(sources, "source_id"))


def selection_row(case: Mapping[str, Any], selection: EvidenceSelection, *, valid_format: bool = True, wall_ms: float = 0.0) -> dict[str, Any]:
    gold_rules, gold_sources = set(case["gold"]["required_rules"]), set(case["gold"]["required_sources"])
    rules, sources = set(selection.selected_rule_ids), set(selection.selected_source_ids)
    rule_tp, source_tp = len(rules & gold_rules), len(sources & gold_sources)
    return {
        "case_id": str(case["id"]), "domain_id": str(case["domain_id"]), "category": str(case["category"]),
        "available_rule_count": len(selection.allowed_rule_ids), "available_source_count": len(selection.allowed_source_ids),
        "gold_rule_ids": sorted(gold_rules), "gold_source_ids": sorted(gold_sources), **selection.to_dict(),
        "rule_tp": rule_tp, "rule_fp": len(rules - gold_rules), "rule_fn": len(gold_rules - rules),
        "source_tp": source_tp, "source_fp": len(sources - gold_sources), "source_fn": len(gold_sources - sources),
        "rule_exact_match": rules == gold_rules, "source_exact_match": sources == gold_sources,
        "missing_evidence": bool((gold_rules - rules) or (gold_sources - sources)), "overselection": bool((rules - gold_rules) or (sources - gold_sources)),
        "invented_ids": len((rules - set(selection.allowed_rule_ids)) | (sources - set(selection.allowed_source_ids))), "format_valid": bool(valid_format), "wall_ms": float(wall_ms),
    }


def aggregate_selection(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    totals = Counter()
    for row in rows:
        for key in ("rule_tp", "rule_fp", "rule_fn", "source_tp", "source_fp", "source_fn", "invented_ids"):
            totals[key] += int(row[key])
    rule_gold, rule_selected = totals["rule_tp"] + totals["rule_fn"], totals["rule_tp"] + totals["rule_fp"]
    source_gold, source_selected = totals["source_tp"] + totals["source_fn"], totals["source_tp"] + totals["source_fp"]
    selected_counts = [len(row["selected_rule_ids"]) + len(row["selected_source_ids"]) for row in rows]
    return {
        "case_count": len(rows), "rule_recall": totals["rule_tp"] / max(1, rule_gold), "rule_precision": totals["rule_tp"] / max(1, rule_selected),
        "rule_exact_match": statistics.mean(float(row["rule_exact_match"]) for row in rows) if rows else 0.0,
        "source_recall": totals["source_tp"] / max(1, source_gold), "source_precision": totals["source_tp"] / max(1, source_selected),
        "source_exact_match": statistics.mean(float(row["source_exact_match"]) for row in rows) if rows else 0.0,
        "missing_evidence_rate": (totals["rule_fn"] + totals["source_fn"]) / max(1, rule_gold + source_gold),
        "overselection_rate": (totals["rule_fp"] + totals["source_fp"]) / max(1, rule_selected + source_selected),
        "mean_selected_ids": statistics.mean(selected_counts) if selected_counts else 0.0,
        "p95_selected_ids": sorted(selected_counts)[max(0, math.ceil(0.95 * len(selected_counts)) - 1)] if selected_counts else 0,
        "invented_ids": totals["invented_ids"], "format_validity": statistics.mean(float(row["format_valid"]) for row in rows) if rows else 0.0,
        "wall_ms": sum(float(row["wall_ms"]) for row in rows),
    }


def grouped_selection(rows: Sequence[Mapping[str, Any]], field: str) -> dict[str, dict[str, Any]]:
    groups: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in rows:
        groups[str(row[field])].append(row)
    return {key: aggregate_selection(values) for key, values in sorted(groups.items())}


def preliminary_gate(validation: Mapping[str, Any], final_a: Mapping[str, Any]) -> dict[str, Any]:
    def passes(value: Mapping[str, Any]) -> bool:
        return bool(value["rule_recall"] >= .90 and value["source_recall"] >= .90 and value["rule_precision"] >= .65 and value["source_precision"] >= .65 and value["missing_evidence_rate"] <= .10 and value["overselection_rate"] <= .30 and value["invented_ids"] == 0 and value["format_validity"] == 1.0)
    return {"validation_passed": passes(validation), "final_a_passed": passes(final_a), "passed": passes(validation) and passes(final_a)}


def development_gate(validation: Mapping[str, Any], final_a: Mapping[str, Any], original: Mapping[str, Any] | None = None) -> dict[str, Any]:
    def passes(value: Mapping[str, Any]) -> bool:
        return bool(
            value["semantic_complete_count"] >= 32 and value["schema_valid_count"] >= 34
            and value["rule_recall"] >= .875 and value["source_recall"] >= .875
            and value["contradiction_recall"] >= .90 and value["counterargument_coverage"] >= .90
            and value["recommendation_presence"] == 1.0 and value["invented_rule_ids"] == 0
            and value["invented_source_ids"] == 0 and value["safety_violations"] == 0
            and value["approval_violations"] == 0
        )
    improvement = True
    if original is not None:
        improvement = bool(
            final_a["semantic_complete_count"] > original["semantic_complete_count"]
            and final_a["rule_precision"] >= original["rule_precision"] - .05
            and final_a["source_precision"] >= original["source_precision"] - .05
        )
    return {"validation_passed": passes(validation), "final_a_passed": passes(final_a), "improves_original": improvement, "passed": passes(validation) and passes(final_a) and improvement}


def order_sensitivity(original: EvidenceSelection, variants: Sequence[EvidenceSelection]) -> dict[str, Any]:
    baseline = set(original.selected_rule_ids) | set(original.selected_source_ids)
    distances = []
    for variant in variants:
        candidate = set(variant.selected_rule_ids) | set(variant.selected_source_ids)
        distances.append(len(baseline ^ candidate) / max(1, len(baseline | candidate)))
    return {"changed": any(value > 0 for value in distances), "jaccard_distance_mean": statistics.mean(distances) if distances else 0.0, "jaccard_distance_max": max(distances, default=0.0)}


def selector_json(raw: str) -> Mapping[str, Any]:
    text = str(raw).strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.I | re.S).strip()
    value = json.loads(text)
    if not isinstance(value, dict):
        raise ValueError("selector_schema_invalid")
    return value


def reserve_seal_audit(path: Path, expected_sha256: str) -> dict[str, Any]:
    stat, digest, line_count = path.stat(), hashlib.sha256(), 0
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk); line_count += chunk.count(b"\n")
    actual = digest.hexdigest()
    return {"path": str(path), "mode": f"{stat.st_mode & 0o777:03o}", "case_count": line_count, "sha256": actual, "sealed": stat.st_mode & 0o777 == 0o600 and line_count == 24 and actual == expected_sha256, "semantic_read": False, "executed": False}
