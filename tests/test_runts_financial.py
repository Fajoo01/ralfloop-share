from copy import deepcopy
from decimal import Decimal
import pytest
from ralfloop_agent.unified_assistant.runts_financial import duplicate_groups, match_reimbursement, assert_approved_totals
from ralfloop_agent.unified_assistant.runts_accounting import reconcile_account


def source(identity,account):
    return {"import_id":identity,"account_id":account,"imported_at":f"2026-01-01T00:00:{identity:02}","source_hash":"a"*64,"raw_rows":[{"date":"2025-01-01","amount":"-210.28"}],"ledger_rows":[("2025-01-01","-210.28")]}


def test_exact_replay_detected_once_without_automatic_owner_or_source_mutation():
    inputs=[source(1,10),source(2,20)]
    before=deepcopy(inputs)
    groups=duplicate_groups(inputs)
    assert len(groups)==1 and groups[0]["projection_occurrences"]==1
    assert groups[0]["preferred_owner"] is None and groups[0]["status"]=="BLOCKED_REVIEW"
    assert groups[0]["original_import_id"]==1 and groups[0]["replay_import_ids"]==[2]
    result=reconcile_account("source",opening="210.35",movements=[{"account_id":"source","importo_signed":groups[0]["projected_source_delta"]}],closing="0.07")
    assert result["status"]=="RECONCILED"
    assert inputs==before


def test_duplicate_requires_all_three_fingerprints_and_owner_requires_evidence():
    for field,changed in [("source_hash","b"*64),("raw_rows",[{"different":True}]),("ledger_rows",[("2025-01-02","-210.28")])]:
        a,b=source(1,10),source(2,20);b[field]=changed
        assert duplicate_groups([a,b])==[]
    evidence={"source_hash":"a"*64,"account_id":20,"source_ref":"synthetic://statement-account-link"}
    g=duplicate_groups([source(1,10),source(2,20)],owner_evidence=[evidence])[0]
    assert g["preferred_owner"]==20 and g["status"]=="VERIFIED_OWNER"


ACCOUNTS={1:{"is_operational_for_association":True},9:{"is_operational_for_association":False}}
ADVANCES=[{"movement_id":1,"account_id":9,"importo_signed":"-100","notes":"obsolete reference 999"}]

def leg(identity,account,amount,ref="synthetic-transfer"):
    return {"movement_id":identity,"account_id":account,"importo_signed":amount,"data_movimento":"2025-11-20","source_ref":"synthetic://statement/"+str(identity),"transfer_reference":ref}


def test_wrong_recorded_id_recovered_only_from_exact_source_transfer_pair():
    rows=[leg(2,1,"-100"),leg(3,9,"100")]
    result=match_reimbursement(ADVANCES,rows,ACCOUNTS,reimbursement_date="2025-11-20")
    assert result["status"]=="VERIFIED_LINK"
    assert result["pairings"][0]["outgoing_ids"]==[2]
    assert result["pairings"][0]["incoming_ids"]==[3]


def test_ambiguous_pairing_and_amount_only_never_auto_resolve():
    rows=[leg(2,1,"-100"),leg(3,9,"100"),leg(4,1,"-100","second"),leg(5,9,"100","second")]
    assert match_reimbursement(ADVANCES,rows,ACCOUNTS,reimbursement_date="2025-11-20")["status"]=="AMBIGUOUS_REIMBURSEMENT_LINK"
    rows=[leg(2,1,"-100",None),leg(3,9,"100",None)]
    assert match_reimbursement(ADVANCES,rows,ACCOUNTS,reimbursement_date="2025-11-20")["status"]=="REIMBURSEMENT_LINK_UNVERIFIED"


def test_date_direction_and_source_are_required():
    for change in ({"importo_signed":"-100"},{"data_movimento":"2025-01-01"},{"source_ref":None}):
        rows=[leg(2,1,"-100"),{**leg(3,9,"100"),**change}]
        assert match_reimbursement(ADVANCES,rows,ACCOUNTS,reimbursement_date="2025-11-20")["status"]=="REIMBURSEMENT_LINK_UNVERIFIED"


def test_approved_totals_must_not_change_for_presentation_corrections():
    rows=[{"side":"entrata","totale":"16366.87"},{"side":"uscita","totale":"15654.17"}]
    approved={"totale_entrate_mappate":"16366.87","totale_uscite_mappate":"15654.17"}
    assert Decimal(assert_approved_totals(rows,approved)["surplus"])==Decimal("712.70")
    with pytest.raises(ValueError,match="APPROVED_TOTALS_CHANGED"):
        assert_approved_totals([*rows,{"side":"uscita","totale":"210.28"}],approved)
