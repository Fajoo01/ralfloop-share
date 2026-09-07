"""Read-only projection of existing accounting decisions; no classifier or executor."""
from decimal import Decimal
import hashlib
import json
import re


def version(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()


def resolve_decision(movement, *, account, categories, mappings, internal_categories, review=None):
    review = review or {}
    chosen = None
    policy = None
    sources = []
    manual = {k:v for k,v in review.items() if v is not None}
    # Explicit decisions precede all automatic/internal fallback mappings.
    if review.get("runts_excluded") or review.get("runts_detached") or review.get("decisione") == "escludi_runts":
        policy = "EXCLUDED_EXISTING_DECISION"
        sources.append("movement_reviews")
    if review.get("runts_category_id_override") is not None:
        chosen = review["runts_category_id_override"]
        sources.append("movement_reviews.runts_category_id_override")
    elif review.get("decisione") == "riclassifica" and review.get("category_id_manuale") is not None:
        chosen = review["category_id_manuale"]
        sources.append("movement_reviews.category_id_manuale")
    else:
        internal_id = review.get("internal_category_id_manuale", movement.get("internal_category_id"))
        if internal_id is None:
            internal_id = movement.get("internal_category_id")
        internal = internal_categories.get(internal_id, {})
        if internal:
            sources.append("categories_internal")
            if internal.get("runts_policy") in {"excluded", "detached"}:
                policy = policy or "EXCLUDED_EXISTING_DECISION"
            chosen = internal.get("runts_category_id")
        if chosen is None:
            chosen = movement.get("category_id")
            if chosen is not None: sources.append("movements.category_id")
    notes = movement.get("notes") or ""
    # Existing legacy decision marker, not an LLM interpretation of the expense.
    personal_debt = bool(re.match(r"^Debito personale\b", notes, re.IGNORECASE))
    if personal_debt:
        sources.append("movements.notes:Debito personale")
        if chosen is not None:
            policy = "CONFLICT_EXISTING_DECISIONS"
        else:
            policy = "NON_MANAGEMENT_PERSONAL_DEBT"
    category = categories.get(chosen, {})
    mapping = mappings.get(chosen)
    if category.get("tipo") in {"trasferimento", "escluso"} or movement.get("is_transfer"):
        policy = policy or "EXCLUDED_EXISTING_DECISION"
    if policy is None:
        policy = "MAPPED_EXISTING_DECISION" if mapping else "MAPPING_MISSING" if chosen is not None else "UNRESOLVED_CLASSIFICATION"
    reimbursement = re.search(r"REIMBURSED:\s*(\d{4}-\d{2}-\d{2})(?:\s+via mov\.\s*([\d ,e]+))?", notes)
    refs = tuple(int(x) for x in re.findall(r"\d+", reimbursement.group(2) or "")) if reimbursement else ()
    return {
        "movement_id":movement["movement_id"],
        "existing_accounting_classification":category.get("codice_interno") or ("PERSONAL_DEBT" if personal_debt else None),
        "existing_category_id":chosen,
        "existing_runts_mapping":mapping,
        "existing_manual_override":manual,
        "advance_type":movement.get("movement_kind"),
        "financial_account":{"account_id":movement["account_id"],"type":account.get("tipo_account"),"entity_operational":bool(account.get("is_operational_for_association")),"requires_reimbursement":bool(account.get("requires_reimbursement"))},
        "source_of_decision":sources,
        "decision_timestamp":review.get("reviewed_at"),
        "decision_version":version({"movement":movement,"review":review,"mapping":mapping}),
        "classification_status":policy,
        "reimbursement_recorded":bool(reimbursement),
        "reimbursement_date":reimbursement.group(1) if reimbursement else None,
        "reimbursement_movement_ids":refs,
        "economic_amount":str(-Decimal(str(movement["importo_signed"]))) if policy=="MAPPED_EXISTING_DECISION" and mapping.get("side")=="uscita" else None,
        "entity_cash_effect":str(movement["importo_signed"]) if account.get("is_operational_for_association") else "0",
        "signed_amount":str(movement["importo_signed"]),
    }


def validate_reimbursement_group(advances, settlements, accounts):
    """Preserve recorded reimbursement; independently check the referenced cash legs."""
    expected = sum((-Decimal(str(m["importo_signed"])) for m in advances), Decimal(0))
    if not advances or not settlements:
        return {"status":"REIMBURSEMENT_LINK_UNVERIFIED","expected":str(expected)}
    # A repayment must debit an entity account OR credit the advancing account.
    account_ids = {m["account_id"] for m in advances}
    valid = all((accounts[m["account_id"]].get("is_operational_for_association") and Decimal(str(m["importo_signed"])) < 0)
                or (m["account_id"] in account_ids and Decimal(str(m["importo_signed"])) > 0) for m in settlements)
    # Do not count both sides of a bank transfer twice.
    entity = [m for m in settlements if accounts[m["account_id"]].get("is_operational_for_association")]
    selected = entity or settlements
    received = sum((abs(Decimal(str(m["importo_signed"]))) for m in selected), Decimal(0))
    return {"status":"VERIFIED" if valid and received==expected else "REIMBURSEMENT_LINK_CONFLICT", "expected":str(expected),"referenced_amount":str(received),"expense_category_preserved":True}


def management_once(decisions, *, verified_settlement_ids=()):
    """Verified repayments affect cash only; never recognize the original expense twice."""
    settled = set(verified_settlement_ids)
    return [d for d in decisions if d["classification_status"]=="MAPPED_EXISTING_DECISION" and d["movement_id"] not in settled]


def project_management_rows(decisions, *, verified_settlement_ids=()):
    from collections import defaultdict
    totals = defaultdict(Decimal)
    for d in management_once(decisions, verified_settlement_ids=verified_settlement_ids):
        if not d["financial_account"]["entity_operational"] and not (d["advance_type"] == "member_advance" and d["financial_account"]["requires_reimbursement"]):
            continue
        mapping = d["existing_runts_mapping"]
        if mapping["side"] not in {"entrata", "uscita"}:
            raise ValueError("existing_mapping_side_invalid")
        key = (mapping["area"],mapping["side"],mapping["voce_codice"],mapping["voce_label"])
        amount = Decimal(d["signed_amount"])
        totals[key] += -amount if mapping["side"]=="uscita" else amount
    return [dict(zip(("area","side","voce_codice","voce_label"),key),totale=str(total)) for key,total in sorted(totals.items())]


def reconcile_account(account_id, *, opening, movements, closing):
    if any(m["account_id"] != account_id for m in movements):
        raise ValueError("financial_account_mismatch")
    delta = sum((Decimal(str(m["importo_signed"])) for m in movements), Decimal(0))
    result = {"account_id":account_id,"movement_delta":str(delta),"opening":None if opening is None else str(opening),"closing":None if closing is None else str(closing)}
    if opening is None or closing is None:
        return {**result,"status":"MISSING_ACCOUNT_BALANCE_EVIDENCE","residual":None}
    residual = Decimal(str(opening))+delta-Decimal(str(closing))
    return {**result,"status":"RECONCILED" if residual==0 else "ACCOUNT_BALANCE_CONFLICT","residual":str(residual)}
