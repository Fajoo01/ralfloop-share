"""Local RUNTS Suite review build. Uses original report logic against SQLite mode=ro.

Run with the existing RUNTS Suite virtualenv; never touches its service or database.
Outputs private review artifacts, not an approved filing.
"""
import argparse
from collections import defaultdict
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--suite", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--year", type=int, required=True)
    parser.add_argument("--practice-id", required=True)
    parser.add_argument("--message-id", required=True)
    parser.add_argument("--message-hash", required=True)
    parser.add_argument("--official-model", type=Path, required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    if args.output.resolve().is_relative_to(root):
        raise ValueError("private_artifacts_must_be_outside_repository")
    os.umask(0o077)
    args.output.mkdir(parents=True, exist_ok=True)
    sys.path.insert(0, str(args.suite))
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session
    from app.api.runts_report import runts_mod_d_draft
    from jinja2 import Environment, FileSystemLoader, StrictUndefined
    from weasyprint import HTML

    db_path = args.suite / "runts_suite.db"
    uri = db_path.resolve().as_uri() + "?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    connection.row_factory = sqlite3.Row
    engine = create_engine("sqlite://", creator=lambda: sqlite3.connect(uri, uri=True))
    captured = {}

    def profile(frame, event, arg):
        if event == "return" and frame.f_code is runts_mod_d_draft.__code__:
            captured.update(frame.f_locals)

    sys.setprofile(profile)
    try:
        with Session(engine) as session:
            draft = runts_mod_d_draft(year=args.year, db=session)
    finally:
        sys.setprofile(None)
    snapshot_row = connection.execute("SELECT snapshot_json FROM runts_prev_year_snapshots WHERE year=?", (args.year-1,)).fetchone()
    if snapshot_row is None:
        raise ValueError("approved_previous_year_snapshot_required")
    previous = json.loads(snapshot_row[0])
    records = [dict(r) for r in connection.execute("""SELECT m.movement_id,m.account_id,m.importo_signed,m.is_transfer,m.movement_kind,
        a.tipo_account,a.is_operational_for_association,a.requires_reimbursement
        FROM movements m JOIN accounts a USING(account_id)
        WHERE data_movimento>=? AND data_movimento<? ORDER BY movement_id""", (f"{args.year}-01-01", f"{args.year+1}-01-01"))]
    money = lambda x: Decimal(str(x or 0))
    opening_cash, opening_bank = money(previous["cassa_finale"]), money(previous["banca_finale"])
    delta = defaultdict(Decimal)
    personal = []
    for row in records:
        if row["is_operational_for_association"]:
            bucket = "cash" if row["tipo_account"].lower() in {"cassa", "libretto"} else "bank" if row["tipo_account"].lower() in {"banca", "conto corrente", "bancario", "postale"} else None
            if bucket is None:
                raise ValueError("unclassified_operational_account")
            delta[bucket] += money(row["importo_signed"])
        elif row["requires_reimbursement"] and row["movement_kind"] == "member_advance":
            personal.append(row)
    cash, bank = opening_cash + delta["cash"], opening_bank + delta["bank"]
    savings = []
    for import_row in connection.execute("SELECT import_id,hash_file FROM imports WHERE source_type='coop_libretto_pdf'"):
        raw_rows = connection.execute("SELECT raw_id,payload_originale_json FROM movements_raw WHERE import_id=? ORDER BY row_number,raw_id", (import_row["import_id"],)).fetchall()
        values = [(r["raw_id"],json.loads(r["payload_originale_json"])) for r in raw_rows]
        values = [(i,r) for i,r in values if str(r.get("data", "")).startswith(str(args.year)) and r.get("saldo_contabile") is not None]
        if values:
            first_id, first = values[0]
            last_id, last = values[-1]
            savings.append({"import_id":import_row["import_id"],"source_file_sha256":import_row["hash_file"],"first_raw_id":first_id,"last_raw_id":last_id,"opening":str(money(first["saldo_contabile"])-money(first["importo"])),"closing":str(money(last["saldo_contabile"]))})
    # No negative-cash clamp or silent reassignment to another account.
    excluded = [{k:r.get(k) for k in ("movement_id", "data", "importo", "category_id", "internal_category_id")} for r in captured["esclusi_transfer"]]
    unmapped = [{k:r.get(k) for k in ("movement_id", "data", "importo", "category_id")} for r in captured["non_mappati"]]
    personal_amount = sum((money(r["importo_signed"]) for r in personal), Decimal(0))
    sys.path.insert(0, str(root))
    from ralfloop_agent.unified_assistant.runts_document_prepare import cash_bridge
    from ralfloop_agent.unified_assistant.memory_service import MemoryDocument, MemoryEntity
    from ralfloop_agent.unified_assistant.platform import SourceRef
    bridge = cash_bridge(opening=opening_cash+opening_bank, management=draft["summary"]["saldo_mappato"],
        excluded=sum((money(r["importo"]) for r in excluded), Decimal(0)),
        unmapped=sum((money(r["importo"]) for r in unmapped), Decimal(0)),
        noncash_management=personal_amount, closing=cash+bank)
    report = {"year":args.year, "summary":draft["summary"], "bridge":bridge,
        "cash_unadjusted":str(cash), "bank_unadjusted":str(bank),
        "savings_statement_evidence":savings,
        "excluded_items":excluded, "unmapped_items":unmapped, "personal_advance_items":personal,
        "classification_verified":False, "writes":0,
        "snapshot_sha256":hashlib.sha256(snapshot_row[0].encode()).hexdigest(),
        "movement_input_sha256":hashlib.sha256(json.dumps(records,sort_keys=True).encode()).hexdigest(),
        "report_code_sha256":hashlib.sha256((args.suite / "app/api/runts_report.py").read_bytes()).hexdigest()}
    report_bytes = json.dumps(report, sort_keys=True, indent=2).encode()
    (args.output / "reconciliation.json").write_bytes(report_bytes)

    overlay = root / "integrations/runts_suite"
    spec = importlib.util.spec_from_file_location("review_modd_builder", overlay / "app/services/runts_modd_builder.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    built = module.build_modd_matrix(draft["righe_mod_d"], previous["rows"])
    unknown = {"current":"Da verificare", "previous":"Da verificare"}
    ctx = {"year":args.year,"prev_year":args.year-1,"ente":"","cf":"", "matrix":built["rows"],
        "totali":built["totali"],"sezioni":built["sezioni"],"capital":built["capital"],
        "capital_verified":False,"previous_capital_verified":False,"taxes":unknown,"after_tax":unknown,
        "capital_totals":dict.fromkeys(("out_current","out_previous","in_current","in_previous"),"Da verificare"),
        "capital_taxes":unknown,"capital_result":unknown,"overall":unknown,
        "cassa_finale":cash,"banca_finale":bank,"cash_bank_verified":False,"cassa_finale_precedente":opening_cash,"banca_finale_precedente":opening_bank,
        "figurative":{k:unknown for k in ("cost_a","cost_b","cost_total","income_a","income_b","income_total")},"ready_to_file":False}
    html = Environment(loader=FileSystemLoader(overlay / "templates"),undefined=StrictUndefined,autoescape=True).get_template("runts_mod_d_pdf.html").render(**ctx)
    (args.output / "modello_d_review.html").write_text(html)
    pdf_path = args.output / "modello_d_review.pdf"
    HTML(string=html, base_url=str(args.suite)).write_pdf(pdf_path)
    pdf = pdf_path.read_bytes()
    extracted = subprocess.run(["pdftotext", "-", "-"],input=pdf,capture_output=True,check=True).stdout.decode()
    if "BOZZA NON DEPOSITABILE" not in extracted or "draft_db" in extracted or "approved_pdf" in extracted:
        raise ValueError("review_pdf_validation_failed")
    now = datetime.now(timezone.utc).isoformat()
    document = SourceRef(system="runtsuite",native_id="document.mod_d.review."+hashlib.sha256(pdf).hexdigest()[:24],locator=str(pdf_path),observed_at=now,content_hash=hashlib.sha256(pdf).hexdigest())
    sources = (SourceRef(system="runts",native_id=args.message_id,locator="https://ista.scrivaniapa.infocamere.it/api/v1/messaggio/"+args.practice_id,observed_at=now,content_hash=args.message_hash),
        SourceRef(system="runts",native_id="ministerial-model-d",locator=str(args.official_model),observed_at=now,content_hash=hashlib.sha256(args.official_model.read_bytes()).hexdigest()),
        SourceRef(system="runtsuite",native_id="reconciliation."+str(args.year),locator=str(args.output / "reconciliation.json"),observed_at=now,content_hash=hashlib.sha256(report_bytes).hexdigest()),document)
    blockers = ["EXCLUDED_MOVEMENTS_CLASSIFICATION_REQUIRED","UNMAPPED_MOVEMENTS","PERSONAL_ADVANCE_TREATMENT_REQUIRED","CAPITAL_AND_TAX_CLASSIFICATION_REQUIRED"]
    if cash < 0: blockers.append("CASH_SAVINGS_CLASSIFICATION_CONFLICT")
    if not bridge["arithmetic_reconciled"]:blockers.append("RECONCILIATION_RESIDUAL")
    from ralfloop_agent.unified_assistant.operational_runtime import BottazziOperationalRuntime
    from ralfloop_agent.unified_assistant.pec_browser_adapter import PecAuthenticatedBrowserAdapter, PecAuthenticatedCdpTransport
    from ralfloop_agent.unified_assistant.pec_runts import RuntsAuthBoundaryProvider
    from ralfloop_agent.unified_assistant.runts_document_prepare import DocumentReviewProposal
    # Providers are composed but PREPARE uses persisted evidence only: no network call.
    with BottazziOperationalRuntime(args.output / "memory.sqlite", pec_provider=PecAuthenticatedBrowserAdapter(PecAuthenticatedCdpTransport()), runts_provider=RuntsAuthBoundaryProvider()) as runtime:
        memory = runtime.memory
        memory.put_document(MemoryDocument.build(document_id=document.native_id,title="Modello D review",body=extracted,source=document))
        memory.put_entity(MemoryEntity.build(entity_id=document.native_id,domain="runts",entity_type="RUNTS_DOCUMENT_REVIEW_INPUT",status="BLOCKED_REVIEW",updated_at=datetime.now(timezone.utc),data={"practice_id":args.practice_id,"message_id":args.message_id,"document":document.model_dump(mode="json"),"blockers":blockers},provenance=sources))
        result = runtime.invoke_pec_runts("runts prepara revisione documento bilancio modello d riconciliazione",{"practice_id":args.practice_id,"message_id":args.message_id,"document_id":document.native_id})
        if result.get("isError") or result.get("selectedCapability") != "runts_prepare_document_review":
            raise ValueError("semantic_prepare_failed")
        proposal = DocumentReviewProposal.model_validate(result["structuredContent"]["proposal"])
    (args.output / "proposal.json").write_text(proposal.model_dump_json(indent=2))
    print(json.dumps({"status":proposal.status,"bridge":bridge,"cash":str(cash),"bank":str(bank),"pdf_sha256":document.content_hash,"corrections":built["corrections"],"blockers":blockers,"writes":0}))


if __name__ == "__main__":
    main()
