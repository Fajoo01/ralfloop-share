from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .existing_skill_adapter import adapter_for
from .storage import append_jsonl, now_iso, sha256_file

ALLOWED_LEVELS = {"deterministic", "deterministic_with_structured_input", "mixed", "non_deterministic", "source_only"}
ALLOWED_JURY = {"never", "unresolved_only", "advisory", "required"}
DEFAULT_MAPPING = Path("config/domain_capability_mapping.yaml")


@dataclass
class CanonicalCapability:
    capability_id: str
    domain_id: str
    display_name: str
    canonical_executor: str | None
    canonical_sources: list[str]
    adapter: str
    deterministic_level: str
    domain_family: str | None = None
    capability_scope: str | None = None
    deterministic_core: str | None = None
    interpretation_layer: str | None = None
    required_inputs: list[str] = field(default_factory=list)
    optional_inputs: list[str] = field(default_factory=list)
    output_schema: dict[str, Any] = field(default_factory=dict)
    side_effects: bool = False
    pure: bool = True
    jury_policy: str = "never"
    jury_allowed: bool = False
    jury_reason_codes: list[str] = field(default_factory=list)
    jury_forbidden_actions: list[str] = field(default_factory=list)
    required_domain_context: list[str] = field(default_factory=list)
    scenario_support: bool = False
    source_precedence_policy: list[str] = field(default_factory=list)
    missing_input_policy: str = "insufficient_input"
    uncovered_case_policy: str = "uncovered_case"
    test_references: list[str] = field(default_factory=list)
    provenance: dict[str, Any] = field(default_factory=dict)
    enabled: bool = True
    raw: dict[str, Any] = field(default_factory=dict)
    mapping_version: str = "1"

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data.pop("raw", None)
        return data


@dataclass
class CapabilityResolution:
    status: str
    capability_id: str | None = None
    domain_id: str | None = None
    confidence: float = 0.0
    reason_codes: list[str] = field(default_factory=list)
    candidates: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class CapabilityExecutionRequest:
    capability_id: str
    inputs: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class CapabilityExecutionResult:
    ok: bool
    status: str
    payload: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class CanonicalCapabilityRegistry:
    def __init__(self, mapping: dict[str, Any], mapping_path: str | Path = DEFAULT_MAPPING) -> None:
        self.mapping = mapping
        self.mapping_path = Path(mapping_path)
        self.mapping_hash = _mapping_hash(mapping)
        self.schema_version = str(mapping.get("schema_version", "1"))
        self.capabilities = {
            cap.capability_id: cap for cap in (
                _capability_from_raw(raw, self.schema_version)
                for raw in mapping.get("capabilities", []) or []
            )
        }

    @classmethod
    def from_mapping(cls, path: str | Path = DEFAULT_MAPPING) -> "CanonicalCapabilityRegistry":
        mapping_path = Path(path)
        data = yaml.safe_load(mapping_path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("invalid_capability_mapping")
        return cls(data, mapping_path)

    def list_capabilities(self) -> list[dict[str, Any]]:
        return [cap.to_dict() for cap in sorted(self.capabilities.values(), key=lambda item: item.capability_id)]

    def get(self, capability_id: str) -> CanonicalCapability:
        try:
            return self.capabilities[capability_id]
        except KeyError as exc:
            raise KeyError(f"capability_missing:{capability_id}") from exc

    def resolve(self, domain_id: str, request: str | dict[str, Any]) -> CapabilityResolution:
        if isinstance(request, dict) and request.get("capability_id"):
            cap_id = str(request["capability_id"])
            cap = self.capabilities.get(cap_id)
            if cap and cap.domain_id == domain_id and cap.enabled:
                return CapabilityResolution("resolved", cap.capability_id, domain_id, 1.0, ["explicit_capability"])
            return CapabilityResolution("missing", cap_id, domain_id, 0.0, ["explicit_capability_missing"])
        text = json.dumps(request, ensure_ascii=False).lower() if isinstance(request, dict) else str(request).lower()
        candidates = []
        for cap in self.capabilities.values():
            if cap.domain_id != domain_id or not cap.enabled:
                continue
            score = 0.0
            reasons = []
            if cap.capability_id.lower() in text:
                score = 1.0
                reasons.append("capability_id")
            elif cap.capability_id == "abc_relcalc" and any(x in text for x in ("relcalc", "calcolatrice relazionale")):
                score = 0.95
                reasons.append("abc_relcalc_trigger")
            elif cap.capability_id == "abc_formula_loop" and any(x in text for x in ("rlfull", "prudential", "formula")):
                score = 0.9
                reasons.append("abc_formula_trigger")
            elif cap.capability_id == "regione_act_dimensioning" and any(x in text for x in ("regione", "act", "dimension")):
                score = 0.9
                reasons.append("cross_bando_dimensioning_trigger")
            if score:
                candidates.append({"capability_id": cap.capability_id, "domain_id": cap.domain_id, "confidence": score, "reason_codes": reasons})
        if not candidates:
            return CapabilityResolution("missing", None, domain_id, 0.0, ["no_capability_match"])
        candidates.sort(key=lambda item: item["confidence"], reverse=True)
        top = candidates[0]
        return CapabilityResolution("resolved", top["capability_id"], domain_id, top["confidence"], top["reason_codes"], candidates)

    def validate_mapping(self) -> dict[str, Any]:
        issues: list[dict[str, Any]] = []
        for cap in self.capabilities.values():
            if cap.deterministic_level not in ALLOWED_LEVELS:
                issues.append({"type": "conflict", "capability_id": cap.capability_id, "field": "deterministic_level"})
            if cap.jury_policy not in ALLOWED_JURY:
                issues.append({"type": "conflict", "capability_id": cap.capability_id, "field": "jury_policy"})
            if cap.deterministic_level == "source_only" and cap.adapter != "source_only":
                issues.append({"type": "conflict", "capability_id": cap.capability_id, "field": "source_only_adapter"})
            if cap.side_effects:
                issues.append({"type": "conflict", "capability_id": cap.capability_id, "field": "side_effects_not_allowed"})
        duplicates = self.detect_duplicates()
        issues.extend(duplicates["duplicate"])
        issues.extend(duplicates["conflict"])
        return {"ok": not issues, "issues": issues, "duplicates": duplicates, "mapping_hash": self.mapping_hash}

    def verify_sources(self) -> dict[str, Any]:
        results = {}
        changed = []
        missing = []
        for cap in self.capabilities.values():
            declared = cap.provenance.get("source_checksums", {}) if isinstance(cap.provenance, dict) else {}
            actual = {}
            for raw in cap.canonical_sources:
                path = _resolve_path(raw)
                if not path.exists():
                    missing.append(raw)
                    actual[raw] = None
                    continue
                actual[raw] = sha256_file(path)
                if declared.get(raw) and declared[raw] != actual[raw]:
                    changed.append(raw)
            results[cap.capability_id] = {"declared": declared, "actual": actual}
        status = "ok" if not changed and not missing else "validation_required"
        return {"ok": status == "ok", "status": status, "changed": sorted(set(changed)), "missing": sorted(set(missing)), "results": results}

    def execute(self, capability_id: str, request: dict[str, Any]) -> dict[str, Any]:
        cap = self.get(capability_id)
        if not cap.enabled:
            return {"ok": False, "status": "disabled", "capability_id": capability_id}
        if cap.adapter == "source_only":
            checksums = {raw: sha256_file(_resolve_path(raw)) for raw in cap.canonical_sources if _resolve_path(raw).exists()}
            return {
                "ok": True,
                "status": "source_only",
                "capability_id": cap.capability_id,
                "domain_id": cap.domain_id,
                "canonical_executor": cap.canonical_executor,
                "deterministic": False,
                "inputs_complete": True,
                "result": {"sources": cap.canonical_sources, "checksums": checksums},
                "unresolved_questions": [],
                "evidence_refs": cap.canonical_sources,
                "source_checksums": checksums,
                "jury_required": cap.jury_policy in {"advisory", "required"},
                "jury_reason_codes": ["evidence_synthesis"] if cap.jury_policy in {"advisory", "required"} else [],
                "error_type": None,
                "error": None,
            }
        adapter = adapter_for(cap.adapter)
        assert adapter is not None
        result = adapter.execute(cap, request).to_dict()
        result["mapping_hash"] = self.mapping_hash
        self._audit(result)
        return result

    def detect_duplicates(self) -> dict[str, list[dict[str, Any]]]:
        executor_to_caps: dict[str, list[str]] = {}
        source_to_domains: dict[str, set[str]] = {}
        for cap in self.capabilities.values():
            if cap.canonical_executor:
                executor_to_caps.setdefault(cap.canonical_executor, []).append(cap.capability_id)
            for source in cap.canonical_sources:
                source_to_domains.setdefault(source, set()).add(cap.domain_id)
        duplicate = [
            {"type": "duplicate", "canonical_executor": executor, "capability_ids": caps}
            for executor, caps in executor_to_caps.items() if len(caps) > 1
        ]
        conflict = [
            {"type": "conflict", "source_path": source, "domains": sorted(domains)}
            for source, domains in source_to_domains.items() if len(domains) > 1
        ]
        alias = [
            {"type": "alias", "source_path": source, "domains": sorted(domains)}
            for source, domains in source_to_domains.items() if len(domains) == 1
        ]
        canonical = [
            {"type": "canonical", "capability_id": cap.capability_id, "canonical_executor": cap.canonical_executor}
            for cap in self.capabilities.values()
        ]
        deprecated = [
            {"type": "deprecated_candidate", "capability_id": "tools/abc_relcalc_from_rlfull_current", "canonical": "abc_formula_loop"}
        ]
        return {"duplicate": duplicate, "conflict": conflict, "alias": alias, "canonical": canonical, "deprecated_candidate": deprecated}

    def parity(self, capability_id: str) -> dict[str, Any]:
        cap = self.get(capability_id)
        if capability_id == "abc_relcalc":
            payload = {"evidence": [{"kind": "observed_fact", "description": "Direct non-pressing invitation accepted", "weight": 18, "confidence": 0.9}]}
            out = self.execute(capability_id, payload)
            from openshell_backend.skills.abc_relcalc import calculate_relation_dict

            canonical = calculate_relation_dict(payload["evidence"])
            passed = all(out["result"].get(k) == canonical.get(k) for k in ("score", "confidence", "bias_flags", "next_safe_action", "evidence_count", "summary"))
            return {"ok": passed, "status": "passed" if passed else "failed", "capability_id": capability_id, "adapter": out["result"], "canonical": canonical}
        if capability_id == "abc_formula_loop":
            out = self.execute(capability_id, {"text": "Arianna propone due chiacchiere."})
            passed = out["ok"] and out["result"].get("formula_version") == "abc_formula_loop_v1"
            return {"ok": passed, "status": "passed" if passed else "failed", "capability_id": capability_id}
        if capability_id == "regione_act_dimensioning":
            out = self.execute(capability_id, {"query": "Come separare beneficiari diretti pubblico indiretti?", "lookup_only": True})
            passed = out["ok"] and out["status"] == "completed" and "beneficiari" in out["result"].get("matched_sections", [])
            return {"ok": passed, "status": "passed" if passed else "failed", "capability_id": capability_id}
        if cap.deterministic_level == "source_only":
            out = self.execute(capability_id, {})
            return {"ok": out["ok"], "status": "passed" if out["ok"] else "failed", "capability_id": capability_id}
        return {"ok": False, "status": "unsupported", "capability_id": capability_id}

    def _audit(self, result: dict[str, Any]) -> None:
        try:
            record = {
                "timestamp": now_iso(),
                "audit_id": hashlib.sha256(json.dumps(result, sort_keys=True, default=str).encode("utf-8")).hexdigest()[:16],
                "capability_id": result.get("capability_id"),
                "domain_id": result.get("domain_id"),
                "canonical_executor": result.get("canonical_executor"),
                "mapping_hash": self.mapping_hash,
                "source_checksums": result.get("source_checksums", {}),
                "inputs_complete": result.get("inputs_complete"),
                "deterministic_complete": result.get("ok") and not result.get("jury_required"),
                "jury_required": result.get("jury_required"),
                "jury_reason_codes": result.get("jury_reason_codes", []),
                "parity_status": None,
                "duplicate_status": None,
                "error_type": result.get("error_type"),
                "error": result.get("error"),
            }
            append_jsonl(Path("logs/domain_capability_mapping.jsonl"), record)
        except OSError:
            pass


def _capability_from_raw(raw: dict[str, Any], mapping_version: str) -> CanonicalCapability:
    return CanonicalCapability(
        capability_id=str(raw["capability_id"]),
        domain_id=str(raw["domain_id"]),
        display_name=str(raw.get("display_name") or raw["capability_id"]),
        canonical_executor=raw.get("canonical_executor"),
        canonical_sources=list(raw.get("canonical_sources") or []),
        adapter=str(raw.get("adapter") or ""),
        deterministic_level=str(raw.get("deterministic_level") or ""),
        required_inputs=list(raw.get("required_inputs") or []),
        optional_inputs=list(raw.get("optional_inputs") or []),
        output_schema=dict(raw.get("output_schema") or {}),
        side_effects=bool(raw.get("side_effects", False)),
        pure=bool(raw.get("pure", True)),
        jury_policy=str(raw.get("jury_policy") or "never"),
        domain_family=raw.get("domain_family"),
        capability_scope=raw.get("capability_scope"),
        deterministic_core=raw.get("deterministic_core"),
        interpretation_layer=raw.get("interpretation_layer"),
        jury_allowed=bool(raw.get("jury_allowed", raw.get("jury_policy") in {"advisory", "required", "unresolved_only"})),
        jury_reason_codes=list(raw.get("jury_reason_codes") or []),
        jury_forbidden_actions=list(raw.get("jury_forbidden_actions") or []),
        required_domain_context=list(raw.get("required_domain_context") or []),
        scenario_support=bool(raw.get("scenario_support", False)),
        source_precedence_policy=list(raw.get("source_precedence_policy") or []),
        missing_input_policy=str(raw.get("missing_input_policy") or "insufficient_input"),
        uncovered_case_policy=str(raw.get("uncovered_case_policy") or "uncovered_case"),
        test_references=list(raw.get("test_references") or []),
        provenance=dict(raw.get("provenance") or {}),
        enabled=bool(raw.get("enabled", True)),
        raw=dict(raw),
        mapping_version=mapping_version,
    )


def _resolve_path(path: str) -> Path:
    raw = Path(path)
    if raw.is_absolute():
        return raw
    return Path(__file__).resolve().parents[2] / raw


def _mapping_hash(mapping: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(mapping, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()
