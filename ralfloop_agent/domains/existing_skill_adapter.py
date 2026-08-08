from __future__ import annotations

import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .storage import now_iso, sha256_file


@dataclass
class AdapterResult:
    ok: bool
    status: str
    capability_id: str
    domain_id: str
    canonical_executor: str | None
    deterministic: bool
    inputs_complete: bool
    result: dict[str, Any] = field(default_factory=dict)
    unresolved_questions: list[str] = field(default_factory=list)
    evidence_refs: list[str] = field(default_factory=list)
    source_checksums: dict[str, str] = field(default_factory=dict)
    jury_required: bool = False
    jury_reason_codes: list[str] = field(default_factory=list)
    error_type: str | None = None
    error: str | None = None
    provenance: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _resolve_path(path: str | None) -> Path | None:
    if not path:
        return None
    p = Path(path)
    return p if p.is_absolute() else _repo_root() / p


def _checksums(paths: list[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for raw in paths:
        path = _resolve_path(raw)
        if path and path.exists() and path.is_file():
            out[raw] = sha256_file(path)
    return out


def _base_result(
    capability: Any,
    *,
    ok: bool,
    status: str,
    deterministic: bool,
    inputs_complete: bool,
    result: dict[str, Any] | None = None,
    unresolved_questions: list[str] | None = None,
    evidence_refs: list[str] | None = None,
    jury_required: bool = False,
    jury_reason_codes: list[str] | None = None,
    error_type: str | None = None,
    error: str | None = None,
    function_name: str | None = None,
    rulebook_sections: list[str] | None = None,
) -> AdapterResult:
    checksums = _checksums(list(getattr(capability, "canonical_sources", []) or []))
    provenance = {
        "capability_id": capability.capability_id,
        "domain_id": capability.domain_id,
        "canonical_executor": capability.canonical_executor,
        "canonical_source_paths": list(getattr(capability, "canonical_sources", []) or []),
        "source_checksums": checksums,
        "mapping_version": getattr(capability, "mapping_version", "1"),
        "function_name": function_name,
        "rulebook_sections": rulebook_sections or [],
        "test_references": list(getattr(capability, "test_references", []) or []),
        "execution_timestamp": now_iso(),
    }
    return AdapterResult(
        ok=ok,
        status=status,
        capability_id=capability.capability_id,
        domain_id=capability.domain_id,
        canonical_executor=capability.canonical_executor,
        deterministic=deterministic,
        inputs_complete=inputs_complete,
        result=result or {},
        unresolved_questions=unresolved_questions or [],
        evidence_refs=evidence_refs or [],
        source_checksums=checksums,
        jury_required=jury_required,
        jury_reason_codes=jury_reason_codes or [],
        error_type=error_type,
        error=error,
        provenance=provenance,
    )


class ABCRelcalcAdapter:
    def execute(self, capability: Any, request: dict[str, Any]) -> AdapterResult:
        if "evidence" not in request:
            return _base_result(
                capability,
                ok=False,
                status="insufficient_input",
                deterministic=True,
                inputs_complete=False,
                unresolved_questions=["evidence"],
                function_name="calculate_relation_dict",
            )
        try:
            from openshell_backend.skills.abc_relcalc import calculate_relation_dict

            data = calculate_relation_dict(
                request.get("evidence") or [],
                base_score=float(request.get("base_score", 50.0)),
            )
            score = int(data["score"])
            normalized = dict(data)
            normalized["range"] = f"{max(0, score - 3)}-{min(100, score + 3)}"
            return _base_result(
                capability,
                ok=True,
                status="completed",
                deterministic=True,
                inputs_complete=True,
                result=normalized,
                evidence_refs=[item.get("description", "") for item in request.get("evidence") or [] if isinstance(item, dict)],
                function_name="calculate_relation_dict",
            )
        except Exception as exc:
            return _base_result(
                capability,
                ok=False,
                status="error",
                deterministic=True,
                inputs_complete=True,
                error_type=type(exc).__name__,
                error=str(exc),
                function_name="calculate_relation_dict",
            )


class ABCFormulaLoopAdapter:
    DETERMINISTIC_FIELDS = (
        "formula_version",
        "rlfull_current",
        "prudential_score",
        "relcalc_score",
        "confidence",
        "operative_range",
        "action",
        "bias_flags",
        "evidence_count",
    )

    def execute(self, capability: Any, request: dict[str, Any]) -> AdapterResult:
        text = str(request.get("text") or "").strip()
        if not text:
            return _base_result(
                capability,
                ok=False,
                status="insufficient_input",
                deterministic=False,
                inputs_complete=False,
                unresolved_questions=["text"],
                function_name="score_text",
            )
        try:
            from openshell_backend.skills import abc_formula_loop

            with tempfile.TemporaryDirectory(prefix="ralf-abc-formula-") as tmp:
                tmp_path = Path(tmp)
                memory_dir = Path(request.get("memory_dir") or tmp_path / "abc_memory")
                cache_path = Path(request.get("evidence_cache_path") or tmp_path / "last_evidence.json")
                raw = abc_formula_loop.score_text(
                    text,
                    memory_dir=memory_dir,
                    evidence_cache_path=cache_path,
                    force_extract=bool(request.get("force_extract", False)),
                )
            normalized = {key: raw.get(key) for key in self.DETERMINISTIC_FIELDS}
            normalized["raw"] = raw
            unresolved = list(raw.get("warnings", []) or [])
            return _base_result(
                capability,
                ok=True,
                status="completed",
                deterministic=not unresolved,
                inputs_complete=True,
                result=normalized,
                unresolved_questions=unresolved,
                evidence_refs=[item.get("manual_ref", "") for item in raw.get("trace", []) if isinstance(item, dict)],
                jury_required=bool(unresolved),
                jury_reason_codes=["mixed_request_unresolved"] if unresolved else [],
                function_name="score_text",
            )
        except Exception as exc:
            return _base_result(
                capability,
                ok=False,
                status="error",
                deterministic=False,
                inputs_complete=True,
                error_type=type(exc).__name__,
                error=str(exc),
                function_name="score_text",
            )


class RulebookLookupAdapter:
    COVERAGE = {
        "beneficiari": ("beneficiari", "separare beneficiari diretti, pubblico, indiretti"),
        "pubblico": ("beneficiari", "separare beneficiari diretti, pubblico, indiretti"),
        "budget": ("budget", "dimensionare solo su contributo concesso e obblighi reali"),
        "contributo": ("budget", "dimensionare solo su contributo concesso e obblighi reali"),
        "cofinanziamento": ("cofinanziamento", "usare spese documentate come cofinanziamento solo se ammissibili e non doppie"),
        "spese documentate": ("cofinanziamento", "usare spese documentate come cofinanziamento solo se ammissibili e non doppie"),
        "date": ("source_required", "non inventare beneficiari, budget, date o destinazioni"),
        "destinazioni": ("source_required", "non inventare beneficiari, budget, date o destinazioni"),
        "documenti": ("documents", "mostrare documenti e bozza prima di allegare"),
        "conferma": ("human_confirmation", "non inviare senza conferma umana"),
        "inviare": ("human_confirmation", "non inviare senza conferma umana"),
    }

    def execute(self, capability: Any, request: dict[str, Any]) -> AdapterResult:
        query = str(request.get("query") or "").strip()
        if not query:
            return _base_result(
                capability,
                ok=False,
                status="insufficient_input",
                deterministic=True,
                inputs_complete=False,
                unresolved_questions=["query"],
                function_name="rulebook_lookup",
            )
        if getattr(capability, "required_domain_context", []) and not request.get("bando_context") and not request.get("lookup_only"):
            return _base_result(
                capability,
                ok=False,
                status="missing_context",
                deterministic=True,
                inputs_complete=False,
                unresolved_questions=["bando_id", "official_rule_scope"],
                function_name="rulebook_lookup",
            )
        if request.get("simulate_conflict"):
            return _base_result(
                capability,
                ok=False,
                status="conflicting_rules",
                deterministic=False,
                inputs_complete=True,
                unresolved_questions=["rule_conflict"],
                jury_required=True,
                jury_reason_codes=["conflicting_sources"],
                function_name="rulebook_lookup",
                rulebook_sections=["simulated_conflict"],
            )
        matched: list[tuple[str, str]] = []
        low = query.lower()
        for token, row in self.COVERAGE.items():
            if token in low and row not in matched:
                matched.append(row)
        if not matched:
            return _base_result(
                capability,
                ok=False,
                status="uncovered_case",
                deterministic=False,
                inputs_complete=True,
                unresolved_questions=["uncovered_case"],
                jury_required=True,
                jury_reason_codes=["incomplete_rules"],
                function_name="rulebook_lookup",
            )
        sections = [section for section, _ in matched]
        answer = "\n".join(f"- {text}" for _, text in matched)
        return _base_result(
            capability,
            ok=True,
            status="completed",
            deterministic=True,
            inputs_complete=True,
            result={
                "matched_sections": sections,
                "answer": answer,
                "rulebook_path": capability.canonical_executor,
                "applicability": {
                    "domain_family": getattr(capability, "domain_family", None),
                    "capability_scope": getattr(capability, "capability_scope", None),
                    "subordinate_to_specific_bando": True,
                    "context": request.get("bando_context") or None,
                },
            },
            evidence_refs=[f"regione_act_dimensioning:{section}" for section in sections],
            function_name="rulebook_lookup",
            rulebook_sections=sections,
        )


def adapter_for(name: str):
    if name == "abc_relcalc":
        return ABCRelcalcAdapter()
    if name == "abc_formula_loop":
        return ABCFormulaLoopAdapter()
    if name == "rulebook_lookup":
        return RulebookLookupAdapter()
    if name == "source_only":
        return None
    raise ValueError(f"unknown_adapter:{name}")
