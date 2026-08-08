from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any


@dataclass
class CalculationValue:
    name: str
    value: Decimal | None
    unit: str = ""
    source_ref: str | None = None
    assumed: bool = False
    assumption_source: str | None = None

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["value"] = str(self.value) if self.value is not None else None
        return data


@dataclass
class CalculationInterpretation:
    requested_value: str
    basis: str | None = None
    source_refs: list[str] = field(default_factory=list)
    assumptions: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class CalculationFormula:
    formula_id: str
    expression: str
    source_refs: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class CalculationPlan:
    requested_value: str
    known_values: dict[str, CalculationValue]
    formula_candidates: list[CalculationFormula] = field(default_factory=list)
    jury_required: bool = False
    jury_reason_codes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["known_values"] = {key: value.to_dict() for key, value in self.known_values.items()}
        data["formula_candidates"] = [formula.to_dict() for formula in self.formula_candidates]
        return data


@dataclass
class CalculationScenario:
    scenario_id: str
    interpretation: str
    formula: str
    inputs: dict[str, Any]
    result: Decimal | None
    source_refs: list[str] = field(default_factory=list)
    assumptions: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["result"] = str(self.result) if self.result is not None else None
        return data


@dataclass
class CalculationExecution:
    deterministic: bool
    formula: str
    result: Decimal | None
    source_refs: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["result"] = str(self.result) if self.result is not None else None
        return data


@dataclass
class CalculationVerification:
    ok: bool
    checks: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class CalculationResult:
    status: str
    answer: str | None = None
    deterministic: bool = False
    jury_required: bool = False
    jury_reason_codes: list[str] = field(default_factory=list)
    scenarios: list[CalculationScenario] = field(default_factory=list)
    preferred_scenario: str | None = None
    human_decision_required: bool = False
    assumptions: list[dict[str, Any]] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)
    limitations: list[str] = field(default_factory=list)
    verification: CalculationVerification = field(default_factory=lambda: CalculationVerification(False))
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["scenarios"] = [scenario.to_dict() for scenario in self.scenarios]
        data["verification"] = self.verification.to_dict()
        return data


@dataclass
class CalculationRequest:
    expression: str | None = None
    goal: str | None = None
    domain_context: dict[str, Any] = field(default_factory=dict)
    values: dict[str, Any] = field(default_factory=dict)
    formula: str | None = None
    scenarios: list[dict[str, Any]] = field(default_factory=list)
    simulate: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class CalculationOrchestrator:
    def calculate(self, request: str | dict[str, Any] | CalculationRequest) -> dict[str, Any]:
        req = self.parse_request(request)
        text = (req.expression or req.goal or "").strip()
        if req.scenarios:
            return self._scenario_result(req).to_dict()
        if not text and not req.formula:
            return CalculationResult("insufficient_input", deterministic=True, jury_required=False, error="expression_missing", verification=CalculationVerification(False, errors=["expression_missing"])).to_dict()
        if _asks_bando_contribution(text) and not req.domain_context.get("bando_id"):
            return CalculationResult("missing_context", deterministic=False, jury_required=True, jury_reason_codes=["missing_context"], limitations=["bando_id_required"], verification=CalculationVerification(False, errors=["missing_context"])).to_dict()
        if _ambiguous_unit(text):
            return CalculationResult("unit_ambiguous", deterministic=False, jury_required=True, jury_reason_codes=["unit_interpretation"], limitations=["unit_ambiguous"], verification=CalculationVerification(False, errors=["unit_ambiguous"])).to_dict()
        if req.formula:
            return self._explicit_formula(req).to_dict()
        percent = _parse_percent_of(text)
        if percent:
            pct, base = percent
            result = (base * pct) / Decimal(100)
            result = self._apply_official_constraints(result, req)
            assumptions = []
            if req.simulate or "simula" in text.lower():
                assumptions.append({"assumed": True, "assumption_source": "user_requested_simulation"})
            return CalculationResult(
                "completed",
                answer=_format_decimal(result),
                deterministic=True,
                jury_required=False,
                assumptions=assumptions,
                sources=list(req.domain_context.get("source_refs", [])),
                verification=CalculationVerification(True, ["arithmetic_verified", "jury_cannot_override_verified_result"]),
            ).to_dict()
        if re.search(r"\d+\s*[%]\s*(di)?\s*$", text.lower()):
            return CalculationResult("insufficient_input", deterministic=True, jury_required=False, error="base_amount_missing", verification=CalculationVerification(False, errors=["base_amount_missing"])).to_dict()
        return CalculationResult("unresolved_formula", deterministic=False, jury_required=True, jury_reason_codes=["ambiguous_formula"], verification=CalculationVerification(False, errors=["formula_missing"])).to_dict()

    def parse_request(self, request: str | dict[str, Any] | CalculationRequest) -> CalculationRequest:
        if isinstance(request, CalculationRequest):
            return request
        if isinstance(request, str):
            return CalculationRequest(expression=request)
        return CalculationRequest(
            expression=request.get("expression"),
            goal=request.get("goal"),
            domain_context=dict(request.get("domain_context") or {}),
            values=dict(request.get("values") or {}),
            formula=request.get("formula"),
            scenarios=list(request.get("scenarios") or []),
            simulate=bool(request.get("simulate", False)),
        )

    def identify_known_values(self, request: CalculationRequest) -> dict[str, CalculationValue]:
        values = {}
        for key, raw in request.values.items():
            values[key] = CalculationValue(key, _to_decimal(raw))
        return values

    def identify_requested_value(self, request: CalculationRequest) -> str:
        text = (request.expression or request.goal or "").lower()
        if "contributo" in text:
            return "contribution"
        return "numeric_result"

    def resolve_domain_context(self, request: CalculationRequest) -> dict[str, Any]:
        return request.domain_context

    def identify_formula_candidates(self, request: CalculationRequest) -> list[CalculationFormula]:
        if request.formula:
            return [CalculationFormula("explicit", request.formula)]
        if _parse_percent_of(request.expression or request.goal or ""):
            return [CalculationFormula("percent_of", "base * percent / 100")]
        return []

    def select_formula_or_request_jury(self, formulas: list[CalculationFormula]) -> dict[str, Any]:
        if len(formulas) == 1:
            return {"formula": formulas[0].to_dict(), "jury_required": False}
        return {"formula": None, "jury_required": True, "jury_reason_codes": ["ambiguous_formula" if formulas else "missing_context"]}

    def validate_units(self, request: CalculationRequest) -> dict[str, Any]:
        return {"ok": not _ambiguous_unit(request.expression or request.goal or "")}

    def detect_missing_inputs(self, request: CalculationRequest) -> list[str]:
        text = request.expression or request.goal or ""
        return ["base_amount"] if re.search(r"\d+\s*[%]\s*(di)?\s*$", text.lower()) else []

    def build_scenarios(self, request: CalculationRequest) -> list[CalculationScenario]:
        return [self._scenario_from_raw(chr(ord("A") + idx), raw) for idx, raw in enumerate(request.scenarios)]

    def execute_deterministically(self, formula: CalculationFormula, values: dict[str, Any]) -> CalculationExecution:
        if formula.formula_id == "percent_of":
            result = (Decimal(str(values["base"])) * Decimal(str(values["percent"]))) / Decimal(100)
            return CalculationExecution(True, formula.expression, result, formula.source_refs)
        return CalculationExecution(False, formula.expression, None, formula.source_refs)

    def verify_result(self, execution: CalculationExecution) -> CalculationVerification:
        return CalculationVerification(execution.result is not None, ["result_present"] if execution.result is not None else [], [] if execution.result is not None else ["result_missing"])

    def _explicit_formula(self, req: CalculationRequest) -> CalculationResult:
        formula = str(req.formula or "")
        values = {key: _to_decimal(value) for key, value in req.values.items()}
        if formula == "base * percent / 100" and {"base", "percent"} <= set(values):
            result = (values["base"] * values["percent"]) / Decimal(100)
            result = self._apply_official_constraints(result, req)
            return CalculationResult("completed", _format_decimal(result), True, False, verification=CalculationVerification(True, ["explicit_formula_verified"] ))
        missing = sorted({"base", "percent"} - set(values)) if formula == "base * percent / 100" else ["supported_formula"]
        return CalculationResult("insufficient_input", deterministic=True, jury_required=False, error="missing_inputs:" + ",".join(missing), verification=CalculationVerification(False, errors=missing))

    def _scenario_result(self, req: CalculationRequest) -> CalculationResult:
        scenarios = self.build_scenarios(req)
        return CalculationResult(
            "scenario_result",
            deterministic=True,
            jury_required=True,
            jury_reason_codes=["multiple_valid_models"],
            scenarios=scenarios,
            preferred_scenario=None,
            human_decision_required=True,
            verification=CalculationVerification(all(s.result is not None for s in scenarios), ["scenarios_calculated"]),
        )

    def _scenario_from_raw(self, scenario_id: str, raw: dict[str, Any]) -> CalculationScenario:
        percent = _to_decimal(raw.get("percent"))
        base = _to_decimal(raw.get("base"))
        result = (base * percent) / Decimal(100) if base is not None and percent is not None else None
        return CalculationScenario(
            scenario_id,
            str(raw.get("interpretation") or ""),
            "base * percent / 100",
            {"base": str(base) if base is not None else None, "percent": str(percent) if percent is not None else None},
            result,
            list(raw.get("source_refs") or []),
            list(raw.get("assumptions") or []),
        )

    def _apply_official_constraints(self, result: Decimal, req: CalculationRequest) -> Decimal:
        context = req.domain_context
        if "official_percentage" in context and "jury_suggested_percentage" in context:
            # Official value has priority; jury suggestion is intentionally ignored.
            pass
        cap = _to_decimal(context.get("max_contribution") if "max_contribution" in context else context.get("cap"))
        if cap is not None and result > cap:
            return cap
        return result


def _parse_percent_of(text: str) -> tuple[Decimal, Decimal] | None:
    cleaned = text.lower().replace(".", "").replace(",", ".")
    match = re.search(r"(\d+(?:\.\d+)?)\s*%\s*(?:di|of)\s*(\d+(?:\.\d+)?)", cleaned)
    if not match:
        return None
    return Decimal(match.group(1)), Decimal(match.group(2))


def _asks_bando_contribution(text: str) -> bool:
    low = text.lower()
    return "contributo" in low and "bando" in low and not _parse_percent_of(low)


def _ambiguous_unit(text: str) -> bool:
    return bool(re.search(r"\b\d+\s*k\b", text.lower()))


def _to_decimal(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value).replace(",", "."))
    except (InvalidOperation, ValueError):
        return None


def _format_decimal(value: Decimal) -> str:
    normalized = value.normalize()
    if normalized == normalized.to_integral():
        return str(int(normalized))
    return format(normalized, "f")
