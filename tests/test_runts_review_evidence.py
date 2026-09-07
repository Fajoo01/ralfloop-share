"""Synthetic evidence tests; no live financial fixtures."""
import hashlib
import json
import sqlite3
from decimal import Decimal

import pytest
from pydantic import ValidationError

from ralfloop_agent.unified_assistant.runts_review_evidence import financial_source_audit
from ralfloop_agent.unified_assistant.runts_document_prepare import (
    AccountReviewEvidence, ApprovedFigures, DocumentReviewContext, prepare_document_review)
from ralfloop_agent.unified_assistant.memory_service import MemoryService
from ralfloop_agent.unified_assistant.platform import SourceRef


SOURCE=SourceRef(system="runts",native_id="synthetic-message",locator="https://example.invalid/message",
                 observed_at="2026-09-07T00:00:00Z",content_hash="a"*64)


def context():
    return DocumentReviewContext(pdf_path="/synthetic/review.pdf",
        approved_figures=ApprovedFigures(income="16366.87",expense="15654.17",surplus="712.70",closing="2283.08"),
        account_reconciliation=(AccountReviewEvidence(account="cash",status="MISSING_BALANCE_EVIDENCE"),),
        evidence_refs=(SOURCE,),generator_version="b"*64,production_db_before="c"*64,production_db_after="c"*64)


def test_production_hash_change_is_rejected():
    data=context().model_dump();data["production_db_after"]="d"*64
    with pytest.raises(ValidationError,match="production_database_changed"):
        DocumentReviewContext.model_validate(data)


@pytest.mark.parametrize("change",[{"closing":"10"},{"evidence_refs":[]},{"opening":None},{"residual":"1"}])
def test_reconciled_account_requires_independent_equation_and_evidence(change):
    values=dict(account="savings",opening="210.35",movement_delta="-210.28",closing="0.07",residual="0",status="RECONCILED",evidence_refs=(SOURCE,))
    with pytest.raises(ValidationError,match="independent_account_evidence_required"):
        AccountReviewEvidence(**(values|change))


def test_empty_blockers_do_not_promote_draft_and_context_is_hash_bound(tmp_path):
    doc=SOURCE.model_copy(update={"system":"runtsuite","native_id":"synthetic-pdf","locator":"/synthetic/review.pdf"})
    with MemoryService(tmp_path / "memory.sqlite") as memory:
        p=prepare_document_review(memory,practice_id="synthetic-practice",message_id=SOURCE.native_id,
            document=doc,provenance=(SOURCE,),blockers=(),review_context=context())
        assert p.status=="BLOCKED_REVIEW" and not p.executable and p.writes==0
        assert p.channel=="RUNTS MESSAGGISTICA" and p.review_context.approved_figures.surplus==Decimal("712.70")
        assert "production_db_hash_unchanged" in p.preconditions
        changed=context().model_dump();changed["generator_version"]="e"*64
        q=prepare_document_review(memory,practice_id="synthetic-practice",message_id=SOURCE.native_id,
            document=doc,provenance=(SOURCE,),blockers=(),review_context=DocumentReviewContext.model_validate(changed))
        assert p.proposal_id!=q.proposal_id
        with pytest.raises(ValidationError):
            type(p).model_validate(p.model_dump()|{"status":"READY_FOR_HUMAN_APPROVAL"})


def test_readonly_source_audit_preserves_db_and_does_not_turn_wallet_into_cash(tmp_path):
    db=tmp_path / "source.sqlite"
    uploads=tmp_path / "data/uploads";uploads.mkdir(parents=True)
    statement=uploads / "synthetic.csv"
    statement.write_text('Data,Netto,Saldo,Impatto sul saldo\n01/01/2025,"4,04","5,25",Accredito\n')
    with sqlite3.connect(db) as c:
        c.executescript("CREATE TABLE imports(import_id,account_id,hash_file,source_type,filename); CREATE TABLE movements_raw(import_id,row_number,raw_id,payload_originale_json); CREATE TABLE movements(import_id,data_movimento,importo_signed);")
        c.execute("INSERT INTO imports VALUES(1,1,?,'csv_generic','synthetic.csv')",(hashlib.sha256(statement.read_bytes()).hexdigest(),))
        raw={"Data":"01/01/2025","Netto":"4,04","Saldo":"5,25","Impatto sul saldo":"Accredito"}
        c.execute("INSERT INTO movements_raw VALUES(1,1,1,?)",(json.dumps(raw),))
        c.execute("INSERT INTO movements VALUES(1,'2025-01-01',4.04)")
    before=hashlib.sha256(db.read_bytes()).hexdigest()
    accounts={1:{"tipo_account":"banca","is_operational_for_association":True},2:{"tipo_account":"wallet","is_operational_for_association":True}}
    with sqlite3.connect(db.as_uri()+"?mode=ro",uri=True) as c:
        c.row_factory=sqlite3.Row
        result=financial_source_audit(c,tmp_path,2025,accounts,[])
    assert hashlib.sha256(db.read_bytes()).hexdigest()==before
    assert result["writes"]==0
    assert result["sources"][0]["statement"]["status"]=="RECONCILED"
    assert result["sources"][0]["source_raw_rows_verified"] is True
    assert result["sources"][0]["ownership_status"]=="OWNERSHIP_UNVERIFIED"
    cash=next(r for r in result["account_scopes"] if r["account"]=="cash")
    assert cash["account_ids"]==[] and cash["closing"] is None and cash["movement_delta"] is None
    assert not result["zero_sections_authorized"]
