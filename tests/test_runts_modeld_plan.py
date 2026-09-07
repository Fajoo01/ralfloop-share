from decimal import Decimal
import pytest
from integrations.runts_suite.app.services.runts_modd_builder import build_modd_matrix, OFFICIAL_ROWS
from ralfloop_agent.unified_assistant.runts_decisions import Authority, DecisionFact
from ralfloop_agent.unified_assistant.runts_modeld_plan import ModelDPlan, MovementPresentation, apply_presentations, final_context, pdf_semantics
from ralfloop_agent.unified_assistant.platform import SourceRef


def plan():
    source=SourceRef(system="synthetic",native_id="decision",locator="synthetic://decision",observed_at="2026-09-07T00:00:00Z",content_hash="a"*64)
    def f(kind,subject,value):return DecisionFact(decision_id=subject,practice_id="synthetic",exercise=2025,fact_type=kind,subject=subject,value=value,source_type=Authority.EXPLICIT_HUMAN,source=source,decision_version=1)
    return ModelDPlan(practice_id="synthetic",message_id="message",exercise=2025,version=1,
        facts=(f("APPROVED_TOTALS","balance",dict(income="16366.87",expense="15654.17",surplus="712.70",cash="0.00",deposits="2283.08")),
            f("IDENTITY","entity",dict(name="Synthetic APS",tax_code="00000000000")),
            f("ECONOMIC_OWNER","savings",dict(legal_owner="member",economic_owner="association",role="ASSOCIATION_ASSET_HELD_BY_MEMBER")),
            f("TAX_PRESENTATION","withholding",dict(own_entity_tax="false",target="E5")),
            f("PRESENTATION","zero_sections",dict(sections="capital,own_taxes,capital_taxes,figurative")),
            f("CLASSIFICATION","2422",dict(target="A5")),f("CLASSIFICATION","2406",dict(target="A5"))),
        movement_presentations=tuple(MovementPresentation(movement_id=i,date="2025-01-01",signed_amount=-amount,source_hash="b"*64,target="A5",label="Uscite diverse di gestione",decision_id=str(i)) for i,amount in ((2422,149),(2406,89))),
        expected_expense_rows={"A1":"48.99","A2":"8574.62","A3":"5109.36","A5":"238.00","E5":"1020.06"},
        zero_sections=("capital","own_taxes","capital_taxes","figurative"))


def matrix():
    rows=[{"voce_codice":c,"voce_label":l,"totale":v,"side":"uscita"} for c,l,v in
          (("A6","Materie prime, sussidiarie, di consumo e merci","48.99"),("A2","Servizi","8574.62"),("A3","Godimento di beni di terzi","5109.36"),("A5","Uscite diverse di gestione","238.00"),("E1","", "303.74"),("E2","","359.40"),("E5","Altre uscite","1020.06"))]
    rows.append({"voce_codice":"RA1","voce_label":"","totale":"16366.87","side":"entrata"})
    rows[0]["voce_label"]=OFFICIAL_ROWS[0][1]
    return build_modd_matrix(rows,[])


def test_tari_2422_2406_maps_to_a5_without_total_change_or_production_mutation():
    p=plan();records=[];decisions=[]
    for o in p.movement_presentations:
        records.append(dict(movement_id=o.movement_id,data_movimento=o.date,importo_signed=str(o.signed_amount),source_ref={"source_hash":o.source_hash}))
        decisions.append(dict(movement_id=o.movement_id,signed_amount=str(o.signed_amount),existing_runts_mapping={"voce_codice":"A7","voce_label":"Servizi"}))
    result,diff=apply_presentations(p,decisions,records)
    assert sum(Decimal(d["amount"]) for d in diff)==238
    assert all(d["to"]=="A5" and d["economic_total_delta"]=="0.00" for d in diff)
    assert all(d["existing_runts_mapping"]["voce_codice"]=="A7" for d in decisions)
    assert all(d["existing_runts_mapping"]["voce_codice"]=="A5" for d in result)
    records[0]["importo_signed"]="-150"
    with pytest.raises(ValueError,match="STALE_CLASSIFICATION_DECISION"):apply_presentations(p,decisions,records)


def test_withholding_stays_e5_zero_own_tax_and_libretto_not_added_twice():
    built=matrix();ctx=final_context(plan(),built,{"figurative":{"cost_a":None,"income_a":None}})
    assert built["rows"]["E5"]["uscite_corrente"]==Decimal("1020.06")
    assert ctx["taxes"]["current"]=="0.00" and ctx["banca_finale"]==Decimal("2283.08")
    assert ctx["cassa_finale"]==0 and ctx["ente"]=="Synthetic APS"
    assert ctx["capital_verified"] and ctx["ready_to_file"]
    assert "Da verificare" not in str(ctx)


def test_approved_totals_are_immutable():
    built=matrix();built["totali"]["uscite_corrente"]+=Decimal("0.07")
    with pytest.raises(ValueError,match="APPROVED_TOTAL_CONFLICT"):final_context(plan(),built,{"figurative":{}})


def test_all_mandatory_zero_sections_preserved():
    built=matrix()
    assert len(built["capital"])==4
    assert built["rows"]["A6"]["uscite_corrente"]==built["rows"]["A7"]["uscite_corrente"]==0
    ctx=final_context(plan(),built,{"figurative":{"cost_a":None,"cost_b":None,"income_a":None,"income_b":None}})
    assert set(ctx["capital_totals"].values())=={"0.00"}
    assert all(v=={"current":"0.00","previous":"0.00"} for v in ctx["figurative"].values())


@pytest.mark.parametrize("placeholder",["Da verificare","BOZZA NON DEPOSITABILE","draft_db"])
def test_no_unverified_placeholder_in_final_pdf(placeholder):
    text="A1 label 48.99 0.00 0.00 0.00\nCassa 0.00\nDepositi bancari e postali 2283.08\nTotale sezione A uscite 13970.97\nTotale uscite 15654.17\nAvanzo d'esercizio 712.70\n"+placeholder
    with pytest.raises(ValueError,match="pdf_not_final_candidate"):pdf_semantics(text)
