"""Data-driven presentation decisions; no practice or movement IDs in algorithms."""
from copy import deepcopy
from decimal import Decimal
import re
from typing import Literal

from pydantic import Field

from .contracts import StrictModel
from .runts_decisions import Authority, DecisionFact, resolve_facts


class MovementPresentation(StrictModel):
    movement_id: int
    date: str
    signed_amount: Decimal = Field(allow_inf_nan=False)
    source_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    target: str = Field(pattern=r"^[A-E][1-9][0-9]?$")
    label: str
    decision_id: str


class ModelDPlan(StrictModel):
    practice_id: str
    message_id: str
    exercise: int
    version: int = Field(ge=1)
    facts: tuple[DecisionFact, ...]
    movement_presentations: tuple[MovementPresentation, ...] = ()
    expected_expense_rows: dict[str, Decimal]
    zero_sections: tuple[Literal["capital","own_taxes","capital_taxes","figurative"], ...]

    def fact(self, kind, subject):
        facts=[f for f in self.facts if f.fact_type==kind and f.subject==subject]
        winner,conflicts=resolve_facts(facts)
        if winner.practice_id!=self.practice_id or winner.exercise!=self.exercise or winner.conflicts or conflicts:
            raise ValueError("SOURCE_CONFLICT")
        return winner

    def validate_scope(self):
        if any(f.practice_id!=self.practice_id or f.exercise!=self.exercise for f in self.facts):
            raise ValueError("decision_scope_mismatch")
        self.fact("APPROVED_TOTALS","balance")
        self.fact("IDENTITY","entity")
        tax=self.fact("TAX_PRESENTATION","withholding")
        if tax.value.get("own_entity_tax")!="false":raise ValueError("tax_presentation_conflict")
        ownership=self.fact("ECONOMIC_OWNER","savings")
        if ownership.value.get("role")!="ASSOCIATION_ASSET_HELD_BY_MEMBER":raise ValueError("ownership_decision_required")
        presentation=self.fact("PRESENTATION","zero_sections")
        if set(self.zero_sections)!=set(presentation.value["sections"].split(",")):
            raise ValueError("zero_section_decision_required")

    def persist(self, memory):
        self.validate_scope()
        for fact in self.facts:fact.persist(memory)


def apply_presentations(plan, decisions, records):
    plan.validate_scope()
    projected=deepcopy(decisions);changes=[]
    for override in plan.movement_presentations:
        fact=plan.fact("CLASSIFICATION",str(override.movement_id))
        if fact.decision_id!=override.decision_id or fact.source_type not in (Authority.EXPLICIT_HUMAN,Authority.RECORDED_DECISION):
            raise ValueError("classification_decision_required")
        if fact.value.get("target")!=override.target:raise ValueError("SOURCE_CONFLICT")
        matches=[d for d in projected if d["movement_id"]==override.movement_id]
        row=next((r for r in records if r["movement_id"]==override.movement_id),None)
        if len(matches)!=1 or row is None or row["data_movimento"]!=override.date or Decimal(str(row["importo_signed"]))!=override.signed_amount or row.get("source_ref",{}).get("source_hash")!=override.source_hash:
            raise ValueError("STALE_CLASSIFICATION_DECISION")
        d=matches[0]
        if Decimal(d["signed_amount"])!=override.signed_amount:raise ValueError("split_presentation_requires_explicit_decision")
        before=d["existing_runts_mapping"]
        d["existing_runts_mapping"]={**before,"voce_codice":override.target,"voce_label":override.label}
        changes.append({"movement_id":override.movement_id,"from":before["voce_codice"],"to":override.target,
                        "amount":str(abs(override.signed_amount)),"economic_total_delta":"0.00","decision_id":fact.storage_id})
    return projected,changes


def final_context(plan, built, context):
    plan.validate_scope()
    figures=plan.fact("APPROVED_TOTALS","balance").value
    income,expense,surplus,cash,deposits=[Decimal(figures[k]) for k in ("income","expense","surplus","cash","deposits")]
    if not all(v.is_finite() for v in (income,expense,surplus,cash,deposits)) or income-expense!=surplus:
        raise ValueError("APPROVED_TOTAL_CONFLICT")
    totals=built["totali"]
    if (totals["entrate_corrente"],totals["uscite_corrente"],totals["saldo_corrente"])!=(income,expense,surplus):raise ValueError("APPROVED_TOTAL_CONFLICT")
    for code,amount in plan.expected_expense_rows.items():
        if built["rows"][code]["uscite_corrente"]!=amount:raise ValueError("GOLDEN_SEMANTIC_CONFLICT")
    identity=plan.fact("IDENTITY","entity").value
    zero={"current":"0.00","previous":"0.00"}
    result={**context,"ente":identity["name"],"cf":identity["tax_code"],"capital_verified":True,"previous_capital_verified":True,
        "taxes":zero,"capital_taxes":zero,"capital_result":zero,
        "capital_totals":dict.fromkeys(("out_current","out_previous","in_current","in_previous"),"0.00"),
        "after_tax":{"current":str(surplus),"previous":str(totals["saldo_precedente"])},
        "overall":{"current":str(surplus),"previous":str(totals["saldo_precedente"])},
        "cassa_finale":cash,"banca_finale":deposits,"cash_bank_verified":True,
        "figurative":{k:zero for k in context["figurative"]},"ready_to_file":True}
    if set(plan.zero_sections)!={"capital","own_taxes","capital_taxes","figurative"}:
        raise ValueError("presentation_evidence_incomplete")
    if any(v for row in built["capital"].values() for k,v in row.items() if k.endswith(("corrente","precedente"))):
        raise ValueError("capital_zero_conflicts_with_classified_rows")
    return result


def pdf_semantics(text):
    """Read numeric cells, not PDF byte metadata; independent of page wrapping."""
    rows={}
    for line in text.splitlines():
        match=re.match(r"^\s*([A-E]\d{1,2})\s",line)
        numbers=re.findall(r"-?\d+\.\d{2}\b",line)
        if match and len(numbers)==4:rows[match[1]]=numbers
    values={}
    for label in ("Cassa","Depositi bancari e postali","Totale sezione A uscite","Totale uscite","Avanzo d'esercizio"):
        lines=[l for l in text.splitlines() if l.strip().startswith(label) and re.search(r"\d+\.\d{2}",l)]
        if not lines:raise ValueError("pdf_semantic_field_missing")
        values[label]=re.findall(r"-?\d+\.\d{2}\b",lines[0])
    if not rows or any(s in text for s in ("Da verificare","BOZZA NON DEPOSITABILE","draft_db","approved_pdf")):
        raise ValueError("pdf_not_final_candidate")
    return {"rows":rows,"values":values}
