from decimal import Decimal
import pytest

from integrations.runts_suite.app.services.runts_modd_builder import build_modd_matrix, OFFICIAL_ROWS
from ralfloop_agent.unified_assistant.runts_document_prepare import cash_bridge, prepare_document_review
from ralfloop_agent.unified_assistant.memory_service import MemoryService
from ralfloop_agent.unified_assistant.platform import SourceRef


def row(code, label, value, side="uscita"):
    return {"voce_codice":code,"voce_label":label,"totale":value,"side":side}


def test_legacy_expense_aliases_use_exact_official_labels_and_preserve_sum():
    data = [row("A6",OFFICIAL_ROWS[0][1],"48.99"),row("A7","Servizi","2795.22"),row("A2","Servizi","6017.40")]
    result = build_modd_matrix(data,[])
    assert result["rows"]["A1"]["uscite_corrente"] == Decimal("48.99")
    assert result["rows"]["A2"]["uscite_corrente"] == Decimal("8812.62")
    assert result["rows"]["A6"]["uscite_corrente"] == 0
    assert not result["rows"]["A6"]["uscite_label"]
    assert result["totali"]["uscite_corrente"] == Decimal("8861.61")


@pytest.mark.parametrize("data", [row("A7","Unobserved label",1),row("Z99","Unknown",1),row("A1","",1,"unknown"),row("A1","",float("nan"))])
def test_unofficial_rows_or_nonfinite_amounts_fail_closed(data):
    with pytest.raises(ValueError):build_modd_matrix([data],[])


def test_all_official_rows_and_capital_rows_exist_without_invented_cash():
    result = build_modd_matrix([],[],capital_rows=[row("4","","20.00","entrata")])
    assert len(result["rows"]) == len(OFFICIAL_ROWS)
    assert len(result["capital"]) == 4
    assert result["capital"]["4"]["entrate_corrente"] == Decimal("20")
    assert result["cassa_finale"] is None and result["banca_finale"] is None
    assert "associati e fondatori" in result["rows"]["B1"]["entrate_label"]


def test_arithmetic_bridge_does_not_authorize_accounting_classification():
    result = cash_bridge(opening="1842.60",management="712.70",excluded="-472.33",unmapped="-20.09",noncash_management="-220.20",closing="2283.08")
    assert result["arithmetic_reconciled"]
    assert Decimal(result["residual"]) == 0
    assert not result["classification_verified"]
    result = cash_bridge(opening=0,management=1,excluded=0,unmapped=0,noncash_management=0,closing=0)
    assert not result["arithmetic_reconciled"]


def test_document_proposal_is_persisted_nonexecutable_and_hash_bound(tmp_path):
    source = SourceRef(system="runts",native_id="synthetic-message",locator="https://example.invalid/message",observed_at="2026-09-06T00:00:00Z",content_hash="1"*64)
    doc = source.model_copy(update={"system":"runtsuite","native_id":"synthetic-document","content_hash":"2"*64})
    with MemoryService(tmp_path / "memory.sqlite") as memory:
        proposal = prepare_document_review(memory,practice_id="synthetic-practice",message_id=source.native_id,document=doc,provenance=(source,doc),blockers=("NEGATIVE_CASH_BALANCE",))
        assert proposal.status == "BLOCKED_REVIEW" and not proposal.executable
        assert proposal.sha256 == "2"*64
        assert "FINAL_DOCUMENT_REVIEW_REQUIRED" in proposal.blockers
        other = prepare_document_review(memory,practice_id="synthetic-practice",message_id=source.native_id,document=doc.model_copy(update={"content_hash":"3"*64}),provenance=(source,doc),blockers=())
        assert other.proposal_id != proposal.proposal_id
        with pytest.raises(ValueError,match="authoritative_document_provenance_required"):
            prepare_document_review(memory,practice_id="synthetic-practice",message_id="unrelated",document=doc,provenance=(source,),blockers=())


def test_runtime_retrieves_semantic_prepare_and_never_calls_external_provider(tmp_path):
    from datetime import datetime, timezone
    from ralfloop_agent.unified_assistant.operational_runtime import BottazziOperationalRuntime
    from ralfloop_agent.unified_assistant.memory_service import MemoryEntity

    class NoNetwork:
        def __getattr__(self, name):raise AssertionError("prepare_must_not_call_network")

    source = SourceRef(system="runts",native_id="synthetic-message",locator="https://example.invalid/message",observed_at="2026-09-06T00:00:00Z",content_hash="1"*64)
    doc = source.model_copy(update={"system":"runtsuite","native_id":"synthetic-document","content_hash":"2"*64})
    with BottazziOperationalRuntime(tmp_path / "memory.sqlite",pec_provider=NoNetwork(),runts_provider=NoNetwork()) as runtime:
        runtime.memory.put_entity(MemoryEntity.build(entity_id=doc.native_id,domain="runts",entity_type="RUNTS_DOCUMENT_REVIEW_INPUT",status="BLOCKED_REVIEW",updated_at=datetime.now(timezone.utc),data={"practice_id":"synthetic-practice","message_id":source.native_id,"document":doc.model_dump(mode="json"),"blockers":["NEGATIVE_CASH_BALANCE"]},provenance=(source,doc)))
        args = {"practice_id":"synthetic-practice","message_id":source.native_id,"document_id":doc.native_id}
        result = runtime.invoke_pec_runts("runts prepara revisione documento bilancio modello d riconciliazione",args)
        assert result["selectedCapability"] == "runts_prepare_document_review"
        assert not result["isError"] and result["structuredContent"]["writes"] == 0
        assert result["structuredContent"]["proposal"]["executable"] is False
        wrong = runtime.pec_runts_mcp.call("runts_prepare_document_review",{**args,"practice_id":"other"})
        assert wrong["isError"]
        malformed = runtime.pec_runts_mcp.call("runts_prepare_document_review",{**args,"practice_id":123})
        assert malformed["structuredContent"]["status"] == "POLICY_DENIED"
