from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
import re
from typing import Any, Iterable, Literal

RuleStatus = Literal["SATISFIED", "UNKNOWN", "VIOLATED"]


@dataclass(frozen=True)
class BandoStructuredEvidence:
    requested_contribution_eur: Decimal | None = None
    requested_contribution_rate: Decimal | None = None
    total_costs_eur: Decimal | None = None
    total_income_eur: Decimal | None = None
    duration_months: int | None = None
    applicant_type: str | None = None
    has_partners: bool | None = None
    documents_present: frozenset[str] = field(default_factory=frozenset)
    documents_manifest_complete: bool = False


@dataclass(frozen=True)
class BandoRuleCheck:
    rule_id: str
    field: str
    status: RuleStatus
    reason: str
    hard: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "rule_id": self.rule_id,
            "field": self.field,
            "status": self.status,
            "reason": self.reason,
            "hard": self.hard,
        }


def _parse_number(raw: str) -> Decimal | None:
    token = raw.strip().replace("€", "").replace(" ", "")
    if not token:
        return None
    if "," in token and "." in token:
        if token.rfind(",") > token.rfind("."):
            token = token.replace(".", "").replace(",", ".")
        else:
            token = token.replace(",", "")
    elif "," in token:
        token = token.replace(".", "").replace(",", ".")
    try:
        return Decimal(token)
    except InvalidOperation:
        return None


def _money_after(text: str, label_pattern: str) -> Decimal | None:
    label = re.search(label_pattern, text, re.I)
    if not label:
        return None
    tail = text[label.end(): label.end() + 160]
    match = re.search(r"(?<![0-9.,])(\d+(?:[. ]\d{3})*(?:,\d+)?)(?![0-9.,])", tail)
    return _parse_number(match.group(1)) if match else None


def extract_bando_structured_evidence(
    application_text: str,
    *,
    budget_text: str = "",
) -> BandoStructuredEvidence:
    app = str(application_text or "")
    budget = str(budget_text or "")
    combined = app + "\n" + budget
    requested = _money_after(
        budget,
        r"Contributo\s+richiesto\s+a\s+Fondazione[^0-9]{0,80}",
    )
    total_costs = _money_after(budget, r"TOTALE\s+COSTI")
    total_income = _money_after(budget, r"TOTALE\s+ENTRATE")
    rate_match = re.search(
        r"%\s+del\s+contributo\s+sul\s+totale\s+dei\s+costi.{0,80}?([0-9]+(?:[.,][0-9]+)?)\s*%",
        budget,
        re.I | re.S,
    )
    rate = (_parse_number(rate_match.group(1)) / Decimal(100)) if rate_match else None
    duration_match = re.search(
        r"durata(?:\s+del\s+progetto)?[^0-9]{0,160}?(\d{1,3})\s*mesi",
        app,
        re.I | re.S,
    )
    duration = int(duration_match.group(1)) if duration_match else None
    applicant_match = re.search(
        r"Forma\s+giuridica\*?\s*\n?\s*([^\n]{3,120})",
        app,
        re.I,
    )
    applicant_type = applicant_match.group(1).strip() if applicant_match else None
    partners_match = re.search(
        r"(?:Il\s+progetto\s+ha\s+dei\s+partner\?|partner[^\n]{0,40}\?)\s*\n?\s*(S[iì]|No)\b",
        app,
        re.I,
    )
    has_partners = None
    if partners_match:
        has_partners = partners_match.group(1).casefold().startswith("s")

    document_patterns = {
        "budget": r"\bBudget\*?\b",
        "logical_framework": r"\bQuadro\s+logico\*?\b",
        "statute": r"\bStatuto\b",
        "declaration": r"\bDichiarazione\b",
        "partnership_agreement": r"\baccordo\s+di\s+partenariato\b",
    }
    documents = frozenset(
        key for key, pattern in document_patterns.items()
        if re.search(pattern, app, re.I)
    )
    manifest_complete = bool(
        re.search(r"\bALLEGATI\b", app, re.I)
        or re.search(r"\b6\.\s*ALLEGATI\b", app, re.I)
    )
    return BandoStructuredEvidence(
        requested_contribution_eur=requested,
        requested_contribution_rate=rate,
        total_costs_eur=total_costs,
        total_income_eur=total_income,
        duration_months=duration,
        applicant_type=applicant_type,
        has_partners=has_partners,
        documents_present=documents,
        documents_manifest_complete=manifest_complete,
    )


def _check_numeric(
    rule_id: str,
    field: str,
    observed: Decimal | None,
    expected: Decimal,
    *,
    relation: str,
) -> BandoRuleCheck:
    if observed is None:
        return BandoRuleCheck(rule_id, field, "UNKNOWN", "numeric_evidence_missing", hard=True)
    ok = observed >= expected if relation == "min" else observed <= expected
    return BandoRuleCheck(
        rule_id,
        field,
        "SATISFIED" if ok else "VIOLATED",
        f"numeric_{relation}_check",
        hard=True,
    )


def evaluate_structured_rule(
    rule: dict[str, Any],
    evidence: BandoStructuredEvidence,
) -> BandoRuleCheck:
    rule_id = str(rule.get("rule_id") or "")
    field_name = str(rule.get("field") or "")
    value = rule.get("value")
    if field_name == "contribution_min_amount":
        return _check_numeric(
            rule_id, field_name, evidence.requested_contribution_eur,
            Decimal(str(value)), relation="min",
        )
    if field_name == "contribution_max_amount":
        return _check_numeric(
            rule_id, field_name, evidence.requested_contribution_eur,
            Decimal(str(value)), relation="max",
        )
    if field_name == "contribution_min_rate":
        return _check_numeric(
            rule_id, field_name, evidence.requested_contribution_rate,
            Decimal(str(value)), relation="min",
        )
    if field_name == "contribution_max_rate":
        return _check_numeric(
            rule_id, field_name, evidence.requested_contribution_rate,
            Decimal(str(value)), relation="max",
        )
    if field_name == "duration" and isinstance(value, dict) and "max_months" in value:
        if evidence.duration_months is None:
            return BandoRuleCheck(rule_id, field_name, "UNKNOWN", "duration_missing", hard=True)
        ok = evidence.duration_months <= int(value["max_months"])
        return BandoRuleCheck(
            rule_id,
            field_name,
            "SATISFIED" if ok else "VIOLATED",
            "duration_max_check",
            hard=True,
        )
    if field_name == "eligibility_applicant_types":
        if not evidence.applicant_type:
            return BandoRuleCheck(rule_id, field_name, "UNKNOWN", "applicant_type_missing", hard=True)
        allowed = " ".join(str(item) for item in (value or [])).casefold()
        observed = evidence.applicant_type.casefold()
        ok = observed in allowed or any(
            token in allowed for token in ("promozione sociale", "aps")
            if token in observed
        )
        return BandoRuleCheck(
            rule_id,
            field_name,
            "SATISFIED" if ok else "VIOLATED",
            "applicant_type_membership",
            hard=True,
        )
    if field_name == "partnership_agreement" and value == "required_if_partnership":
        if evidence.has_partners is False:
            return BandoRuleCheck(rule_id, field_name, "SATISFIED", "not_applicable_no_partners", hard=True)
        if evidence.has_partners is None:
            return BandoRuleCheck(rule_id, field_name, "UNKNOWN", "partnership_state_missing", hard=True)
        if "partnership_agreement" in evidence.documents_present:
            return BandoRuleCheck(rule_id, field_name, "SATISFIED", "partnership_agreement_present", hard=True)
        status: RuleStatus = "VIOLATED" if evidence.documents_manifest_complete else "UNKNOWN"
        return BandoRuleCheck(rule_id, field_name, status, "partnership_agreement_missing", hard=True)
    if field_name == "documents" and isinstance(value, list):
        required = {str(item) for item in value}
        if evidence.has_partners is False:
            required.discard("partnership_agreement")
        missing = required - set(evidence.documents_present)
        if not missing:
            return BandoRuleCheck(rule_id, field_name, "SATISFIED", "mandatory_documents_present", hard=False)
        return BandoRuleCheck(rule_id, field_name, "UNKNOWN", "mandatory_documents_need_review", hard=False)
    return BandoRuleCheck(rule_id, field_name, "UNKNOWN", "semantic_or_unbound_rule", hard=False)


def evaluate_structured_rules(
    rules: Iterable[dict[str, Any]],
    evidence: BandoStructuredEvidence,
) -> tuple[BandoRuleCheck, ...]:
    return tuple(evaluate_structured_rule(rule, evidence) for rule in rules)


def budget_balance_check(evidence: BandoStructuredEvidence) -> BandoRuleCheck:
    if evidence.total_costs_eur is None or evidence.total_income_eur is None:
        return BandoRuleCheck("budget_balance", "budget_balance", "UNKNOWN", "budget_totals_missing", hard=False)
    if evidence.total_income_eur == evidence.total_costs_eur:
        return BandoRuleCheck("budget_balance", "budget_balance", "SATISFIED", "budget_balanced", hard=False)
    return BandoRuleCheck("budget_balance", "budget_balance", "VIOLATED", "budget_totals_mismatch", hard=False)


def hard_rule_violation(checks: Iterable[BandoRuleCheck]) -> bool:
    return any(check.hard and check.status == "VIOLATED" for check in checks)


__all__ = [
    "BandoRuleCheck",
    "BandoStructuredEvidence",
    "budget_balance_check",
    "evaluate_structured_rule",
    "evaluate_structured_rules",
    "extract_bando_structured_evidence",
    "hard_rule_violation",
]
