from __future__ import annotations

import hashlib
import json
from typing import Any, Iterable, Mapping

from .recursive_mas_domain_provenance import (
    EvidencePacket,
    build_evidence_packet,
    detect_demo_contamination,
    parse_provenance_canonical_record,
    serialize_with_evidence_packet,
)
from .recursive_mas_domain_serialization import DomainSerializationError, ID_MENTION_RE


QWEN3_SOLVER_MODEL_ID = "Qwen/Qwen3-1.7B"
QWEN3_SOLVER_REVISION = "70d244cc86ccca08cf5af4e1e306ecf908b1ad5e"
QWEN3_SOLVER_HIDDEN_SIZE = 2048
CANARY_CASE_IDS = (
    "domain_adapter_stra_02_02",
    "domain_adapter_stra_03_01",
    "domain_adapter_conf_04_01",
    "domain_adapter_conf_04_03",
    "domain_adapter_reco_01_03",
    "domain_adapter_reco_01_04",
    "domain_adapter_inco_02_02",
    "domain_adapter_inco_03_02",
)


def stable_sha256(value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def validate_qwen3_metadata(audit: Mapping[str, Any]) -> dict[str, Any]:
    errors = []
    expected = {
        "model_id": QWEN3_SOLVER_MODEL_ID,
        "requested_revision": QWEN3_SOLVER_REVISION,
        "effective_revision": QWEN3_SOLVER_REVISION,
        "model_type": "qwen3",
        "architecture": "Qwen3ForCausalLM",
        "hidden_size": QWEN3_SOLVER_HIDDEN_SIZE,
        "num_hidden_layers": 28,
        "vocab_size": 151936,
        "tokenizer_class": "Qwen2Tokenizer",
        "license": "apache-2.0",
    }
    for field, value in expected.items():
        if audit.get(field) != value:
            errors.append(f"metadata_mismatch:{field}")
    if not audit.get("revision_pinned"):
        errors.append("revision_not_pinned")
    if audit.get("missing_required_files"):
        errors.append("required_files_missing")
    return {
        "ok": not errors,
        "errors": errors,
        "hidden_embedding_compatible": audit.get("hidden_size") == QWEN3_SOLVER_HIDDEN_SIZE,
    }


def detect_qwen3_template_modes(template: str) -> dict[str, Any]:
    value = str(template)
    return {
        "thinking_supported": "enable_thinking" in value and "<think>" in value and "</think>" in value,
        "non_thinking_supported": "enable_thinking is false" in value,
        "non_thinking_argument": {"enable_thinking": False},
        "assistant_marker": "<|im_start|>assistant" if "<|im_start|>assistant" in value else None,
    }


def final_only_from_thinking(raw: str) -> dict[str, Any]:
    value = str(raw)
    if "<think>" not in value:
        return {"final": value.strip(), "reasoning_discarded": False, "thinking_closed": True}
    if "</think>" not in value:
        return {"final": "", "reasoning_discarded": True, "thinking_closed": False}
    return {
        "final": value.rsplit("</think>", 1)[1].strip(),
        "reasoning_discarded": True,
        "thinking_closed": True,
    }


def _deduplicate(values: Iterable[str]) -> list[str]:
    output = []
    for value in values:
        item = str(value)
        if item and item not in output:
            output.append(item)
    return output


def _error_ids(errors: Any, allowed: set[str]) -> list[str]:
    serialized = json.dumps(errors or [], ensure_ascii=False)
    return _deduplicate(identifier for identifier in ID_MENTION_RE.findall(serialized) if identifier in allowed)


def build_gold_packet(case: Mapping[str, Any], trace: Mapping[str, Any]) -> EvidencePacket:
    planner = trace["planner_target"]
    critic = trace["critic_target"]
    planner_rules = _deduplicate(planner.get("rules_selected") or [])
    planner_sources = _deduplicate(planner.get("sources_selected") or [])
    challenged_rules = _error_ids(critic.get("rule_application_errors"), set(planner_rules))
    challenged_sources = _error_ids(critic.get("source_provenance_errors"), set(planner_sources))
    confirmed_rules = [item for item in planner_rules if item not in challenged_rules]
    confirmed_sources = [item for item in planner_sources if item not in challenged_sources]
    return build_evidence_packet(
        allowed_rule_ids=[item["rule_id"] for item in case["rules"]],
        allowed_source_ids=[item["source_id"] for item in case["sources"]],
        planner_output={"rule_ids": planner_rules, "source_ids": planner_sources},
        critic_output={
            "confirmed_rule_ids": confirmed_rules,
            "challenged_rule_ids": challenged_rules,
            "confirmed_source_ids": confirmed_sources,
            "challenged_source_ids": challenged_sources,
            "contradictions": critic.get("contradictions") or [],
        },
    )


def build_real_packet(
    case: Mapping[str, Any], planner_output: Mapping[str, Any], critic_output: Mapping[str, Any]
) -> EvidencePacket:
    return build_evidence_packet(
        allowed_rule_ids=[item["rule_id"] for item in case["rules"]],
        allowed_source_ids=[item["source_id"] for item in case["sources"]],
        planner_output=planner_output,
        critic_output=critic_output,
    )


def fixture_hash_record(case: Mapping[str, Any], trace: Mapping[str, Any], packet: EvidencePacket) -> dict[str, str]:
    domain = {
        key: case[key]
        for key in ("domain_id", "domain_version", "facts", "rules", "sources", "known_contradictions")
    }
    return {
        "case_id": str(case["id"]),
        "input_hash": stable_sha256(case),
        "domain_hash": stable_sha256(domain),
        "gold_hash": stable_sha256(trace),
        "evidence_packet_hash": stable_sha256(packet.to_dict()),
    }


def evaluate_solver_final(
    *,
    final_text: str,
    case: Mapping[str, Any],
    trace: Mapping[str, Any],
    packet: EvidencePacket,
) -> dict[str, Any]:
    output: dict[str, Any] = {
        "raw_syntax_valid": False,
        "semantic_complete": False,
        "final_schema_valid": False,
        "contradiction_inclusion": False,
        "counterargument_coverage": False,
        "uncertainty_presence": False,
        "recommendation_presence": False,
        "argument_level_provenance_coverage": 0.0,
        "provenance_ids_emitted": _deduplicate(ID_MENTION_RE.findall(str(final_text))),
        "structured_provenance_regenerated": False,
        "foreign_ids_generated": [],
        "foreign_ids_accepted": [],
        "demo_contamination": detect_demo_contamination(final_text),
        "parse_error": None,
        "error_class": None,
    }
    allowed = set(packet.allowed_rule_ids) | set(packet.allowed_source_ids)
    output["foreign_ids_generated"] = [item for item in output["provenance_ids_emitted"] if item not in allowed]
    try:
        record = parse_provenance_canonical_record(final_text, packet)
        output["raw_syntax_valid"] = True
    except DomainSerializationError as exc:
        output["parse_error"] = exc.code
        output["error_class"] = "solver_semantic_error"
        return output
    output["semantic_complete"] = bool(
        record.position
        and record.support
        and record.counter
        and record.uncertainty
        and record.recommendation
        and 0 <= record.confidence <= 1
    )
    output["contradiction_inclusion"] = bool(record.counter)
    output["counterargument_coverage"] = bool(record.counter)
    output["uncertainty_presence"] = bool(record.uncertainty)
    output["recommendation_presence"] = bool(record.recommendation)
    arguments = [*record.support, *record.counter]
    output["argument_level_provenance_coverage"] = (
        sum(bool(item.evidence_slots) for item in arguments) / len(arguments) if arguments else 0.0
    )
    try:
        serialized = serialize_with_evidence_packet(
            record,
            {"domain_id": str(case["domain_id"]), "question": str(case["question"])},
            packet,
        )
    except DomainSerializationError as exc:
        output["parse_error"] = exc.code
        output["error_class"] = "serialization_error"
        return output
    output["final_schema_valid"] = bool(serialized["validation"]["ok"])
    output["argument_level_provenance_coverage"] = (
        0.0 if serialized["metadata"]["argument_level_provenance_missing"] else 1.0
    )
    output["payload"] = serialized["payload"]
    output["metadata"] = serialized["metadata"]
    if not output["semantic_complete"]:
        output["error_class"] = "solver_semantic_error"
    return output


def classify_pipeline_error(*, packet_complete: bool, solver_valid: bool, serialization_valid: bool) -> str | None:
    if not packet_complete:
        return "upstream_provenance_error"
    if not solver_valid:
        return "solver_semantic_error"
    if not serialization_valid:
        return "serialization_error"
    return None


def aggregate_solver_results(results: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    items = list(results)
    total = len(items)
    if total != 8:
        raise ValueError("qwen3_solver_gate_requires_eight_cases")
    mean = lambda field: sum(float(bool(item[field])) for item in items) / total
    metrics = {
        "total": total,
        "semantic_complete_count": sum(bool(item["semantic_complete"]) for item in items),
        "final_schema_valid_count": sum(bool(item["final_schema_valid"]) for item in items),
        "contradiction_inclusion": mean("contradiction_inclusion"),
        "counterargument_coverage": mean("counterargument_coverage"),
        "uncertainty_presence": mean("uncertainty_presence"),
        "recommendation_presence": mean("recommendation_presence"),
        "foreign_ids_accepted": sum(len(item["foreign_ids_accepted"]) for item in items),
        "foreign_ids_generated": sum(len(item["foreign_ids_generated"]) for item in items),
        "provenance_id_mentions": sum(len(item["provenance_ids_emitted"]) for item in items),
        "provenance_regeneration_count": sum(bool(item["structured_provenance_regenerated"]) for item in items),
        "demo_contamination_count": sum(bool(item["demo_contamination"]) for item in items),
        "solver_semantic_errors": sum(item.get("error_class") == "solver_semantic_error" for item in items),
        "serialization_errors": sum(item.get("error_class") == "serialization_error" for item in items),
    }
    return metrics


def gold_solver_gate(metrics: Mapping[str, Any]) -> dict[str, Any]:
    passed = bool(
        int(metrics.get("semantic_complete_count") or 0) >= 7
        and int(metrics.get("final_schema_valid_count") or 0) == 8
        and float(metrics.get("contradiction_inclusion") or 0) == 1.0
        and float(metrics.get("counterargument_coverage") or 0) == 1.0
        and float(metrics.get("uncertainty_presence") or 0) == 1.0
        and float(metrics.get("recommendation_presence") or 0) == 1.0
        and int(metrics.get("foreign_ids_accepted") or 0) == 0
        and int(metrics.get("provenance_regeneration_count") or 0) == 0
        and int(metrics.get("demo_contamination_count") or 0) == 0
    )
    return {"passed": passed, "classification": "gold_solver_gate_passed" if passed else "qwen3_solver_base_insufficient"}


def end_to_end_gate(metrics: Mapping[str, Any]) -> dict[str, Any]:
    passed = bool(
        int(metrics.get("semantic_complete_count") or 0) >= 7
        and int(metrics.get("final_schema_valid_count") or 0) == 8
        and float(metrics.get("final_rule_accuracy") or 0) >= 0.875
        and float(metrics.get("final_source_accuracy") or 0) >= 0.875
        and float(metrics.get("contradiction_inclusion") or 0) == 1.0
        and float(metrics.get("counterargument_coverage") or 0) == 1.0
        and int(metrics.get("foreign_ids_accepted") or 0) == 0
        and int(metrics.get("provenance_regeneration_count") or 0) == 0
        and int(metrics.get("demo_contamination_count") or 0) == 0
        and int(metrics.get("safety_violations") or 0) == 0
    )
    return {
        "passed": passed,
        "classification": (
            "qwen3_solver_sufficient_for_micro_overfit" if passed else "qwen3_solver_end_to_end_insufficient"
        ),
    }
