from decimal import Decimal
import pytest
from ralfloop_agent.unified_assistant.runts_accounting import resolve_decision, project_management_rows, reconcile_account, validate_reimbursement_group

CATEGORIES = {25:{"codice_interno":"PDC_AFFILIAZIONE_ARCI","tipo":"uscita"},30:{"codice_interno":"EXISTING_OTHER","tipo":"uscita"}}
MAPPINGS = {25:{"area":"A","side":"uscita","voce_codice":"A2","voce_label":"Servizi"}}
ENTITY = {"tipo_account":"banca","is_operational_for_association":1,"requires_reimbursement":0}
PERSONAL = {"tipo_account":"banca","is_operational_for_association":0,"requires_reimbursement":1}

def movement(identity=1, **kw):
    return {"movement_id":identity,"account_id":9,"importo_signed":-10,"category_id":25,"internal_category_id":None,"movement_kind":"member_advance","notes":None,"is_transfer":False,**kw}

def resolve(m, account=PERSONAL, **kw):
    return resolve_decision(m,account=account,categories=CATEGORIES,mappings=MAPPINGS,internal_categories=kw.pop("internal_categories",{}),**kw)


def test_explicit_manual_decision_precedes_internal_fallback():
    m=movement(category_id=30,internal_category_id=5)
    d=resolve(m,review={"decisione":"riclassifica","category_id_manuale":25},internal_categories={5:{"runts_policy":"mapped","runts_category_id":30}})
    assert d["existing_category_id"]==25
    assert d["classification_status"]=="MAPPED_EXISTING_DECISION"


@pytest.mark.parametrize("identity,amount",[(2544,-135.20),(2576,-85),(99991,-135.20)])
def test_existing_affiliation_and_advance_are_independent(identity,amount):
    d=resolve(movement(identity,importo_signed=amount))
    assert d["existing_accounting_classification"]=="PDC_AFFILIAZIONE_ARCI"
    assert d["advance_type"]=="member_advance"
    assert d["existing_runts_mapping"]["voce_codice"]=="A2"
    assert d["entity_cash_effect"]=="0"
    assert Decimal(project_management_rows([d])[0]["totale"]) == -Decimal(str(amount))


@pytest.mark.parametrize("identity",[2488,99992])
def test_legacy_personal_debt_decision_is_recovered_not_reclassified(identity):
    d=resolve(movement(identity,category_id=None,notes="Debito personale Synthetic da TransferWise",importo_signed=-20.09),account=ENTITY)
    assert d["classification_status"]=="NON_MANAGEMENT_PERSONAL_DEBT"
    assert d["existing_runts_mapping"] is None
    assert d["decision_timestamp"] is None  # No fabricated original timestamp.
    assert d["decision_version"] and "movements.notes:Debito personale" in d["source_of_decision"]
    assert d["entity_cash_effect"]=="-20.09"
    assert project_management_rows([d])==[]


def test_existing_classification_without_mapping_is_not_reopened():
    d=resolve(movement(category_id=30))
    assert d["existing_accounting_classification"]=="EXISTING_OTHER"
    assert d["classification_status"]=="MAPPING_MISSING"
    assert resolve(movement(category_id=None))["classification_status"]=="UNRESOLVED_CLASSIFICATION"


def test_verified_repayment_does_not_duplicate_expense():
    original=movement(1,importo_signed=-100)
    repayment=movement(2,account_id=1,importo_signed=-100,movement_kind="reimbursement")
    proof=validate_reimbursement_group([original],[repayment],{9:PERSONAL,1:ENTITY})
    assert proof["status"]=="VERIFIED"
    output=project_management_rows([resolve(original),resolve(repayment,account=ENTITY)],verified_settlement_ids=(2,))
    assert Decimal(output[0]["totale"])==100


def test_recorded_reimbursement_is_preserved_but_invalid_links_do_not_authorize_dedup():
    m=movement(1,notes="Spesa per ente da conto socio | REIMBURSED: 2025-11-20 via mov. 2 e 3")
    d=resolve(m)
    assert d["reimbursement_recorded"] and d["reimbursement_movement_ids"]==(2,3)
    proof=validate_reimbursement_group([m],[movement(2,importo_signed=-5),movement(3,importo_signed=-5)],{9:PERSONAL})
    assert proof["status"]=="REIMBURSEMENT_LINK_CONFLICT"
    assert d["existing_accounting_classification"]=="PDC_AFFILIAZIONE_ARCI"


def test_each_financial_account_reconciles_without_cash_savings_compensation():
    savings=reconcile_account(3,opening="210.35",movements=[movement(account_id=3,importo_signed="-210.28")],closing="0.07")
    assert savings["status"]=="RECONCILED"
    wrong=reconcile_account(3,opening="23.47",movements=[movement(account_id=3,importo_signed="-210.28")],closing="0.07")
    assert wrong["status"]=="ACCOUNT_BALANCE_CONFLICT"
    missing=reconcile_account(1,opening=None,movements=[],closing=None)
    assert missing["status"]=="MISSING_ACCOUNT_BALANCE_EVIDENCE"
    with pytest.raises(ValueError,match="financial_account_mismatch"):
        reconcile_account(3,opening=0,movements=[movement(account_id=1)],closing=0)
