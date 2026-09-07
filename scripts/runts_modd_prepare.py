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
import re
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
    parser.add_argument("--approved-income", required=True)
    parser.add_argument("--approved-expense", required=True)
    parser.add_argument("--approved-surplus", required=True)
    parser.add_argument("--approved-closing", required=True)
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
    records = [dict(r) for r in connection.execute("""SELECT m.movement_id,m.account_id,m.import_id,m.data_movimento,m.importo_signed,m.is_transfer,m.movement_kind,m.notes,m.category_id,m.internal_category_id,m.contropartita,m.descrizione_originale,
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
    from ralfloop_agent.unified_assistant.runts_accounting import resolve_decision, validate_reimbursement_group, reconcile_account, project_management_rows, version
    from ralfloop_agent.unified_assistant.runts_financial import duplicate_groups, match_reimbursement, assert_approved_totals
    from ralfloop_agent.unified_assistant.memory_service import MemoryDocument, MemoryEntity
    from ralfloop_agent.unified_assistant.platform import SourceRef
    bridge = cash_bridge(opening=opening_cash+opening_bank, management=draft["summary"]["saldo_mappato"],
        excluded=sum((money(r["importo"]) for r in excluded), Decimal(0)),
        unmapped=sum((money(r["importo"]) for r in unmapped), Decimal(0)),
        noncash_management=personal_amount, closing=cash+bank)
    accounts = {r["account_id"]:dict(r) for r in connection.execute("SELECT * FROM accounts")}
    import_hashes = dict(connection.execute("SELECT import_id,hash_file FROM imports"))
    for r in records:
        if import_hashes.get(r["import_id"]):
            r["source_ref"] = {"import_id":r["import_id"],"source_hash":import_hashes[r["import_id"]],"movement_id":r["movement_id"]}
        text = re.sub(r"\s+", "", (r.get("contropartita") or "") + " " + (r.get("descrizione_originale") or "")).upper()
        matches = []
        for identity, account in accounts.items():
            alias = re.sub(r"\s+", "", account.get("iban_o_alias") or "").upper()
            if re.fullmatch(r"[A-Z]{2}\d{2}[A-Z0-9]{10,30}", alias) and alias in text:
                matches.append(identity)
        if len(matches)==1:r["counterparty_account_id"]=matches[0]
    categories = {r["category_id"]:dict(r) for r in connection.execute("SELECT * FROM categories")}
    mappings = {r["category_id"]:dict(r) for r in connection.execute("SELECT * FROM runts_mappings WHERE attivo=1")}
    internals = {r["internal_category_id"]:dict(r) for r in connection.execute("SELECT * FROM categories_internal WHERE attiva=1")}
    reviews = {r["movement_id"]:{k:r[k] for k in r.keys() if k != "reviewed_by"} for r in connection.execute("SELECT * FROM movement_reviews")}
    decisions = [resolve_decision(r, account=accounts[r["account_id"]],categories=categories,mappings=mappings,internal_categories=internals,review=reviews.get(r["movement_id"])) for r in records]
    projection_decisions = []
    for r, decision in zip(records, decisions):
        splits = captured["splits_by_movement"].get(r["movement_id"])
        if not splits:
            projection_decisions.append(decision)
            continue
        for split in splits:
            split_input = {**r,"category_id":split["category_id"],"internal_category_id":None,"notes":None,"is_transfer":False,"importo_signed":split["importo_signed"]}
            override = {"runts_excluded":1} if not split["runts_includi"] else None
            projection_decisions.append(resolve_decision(split_input,account=accounts[r["account_id"]],categories=categories,mappings=mappings,internal_categories=internals,review=override))
    decision_by_id = {d["movement_id"]:d for d in decisions}
    movement_by_id = {r["movement_id"]:r for r in records}
    reimbursement_groups = defaultdict(list)
    for d in decisions:
        if d["reimbursement_recorded"] and (d["financial_account"]["entity_operational"] or d["financial_account"]["requires_reimbursement"]):
            reimbursement_groups[tuple(d["reimbursement_movement_ids"])].append(movement_by_id[d["movement_id"]])
    reimbursements = []
    for ids, advances in reimbursement_groups.items():
        checks = validate_reimbursement_group(advances,[movement_by_id[i] for i in ids if i in movement_by_id],accounts)
        recorded_dates={decision_by_id[a["movement_id"]]["reimbursement_date"] for a in advances}
        recovery=match_reimbursement(advances,records,accounts,reimbursement_date=next(iter(recorded_dates))) if len(recorded_dates)==1 else {"status":"REIMBURSEMENT_LINK_UNVERIFIED","pairings":[]}
        reimbursements.append({"advance_ids":[m["movement_id"] for m in advances],"recorded_settlement_ids":ids,"recorded_reference_validation":checks,**recovery})
    verified_settlement_ids = {i for r in reimbursements if r["status"]=="VERIFIED_LINK" for p in r["pairings"] for i in (*p["outgoing_ids"],*p["incoming_ids"])}
    projected_rows = project_management_rows(projection_decisions,verified_settlement_ids=verified_settlement_ids)
    approved_totals=assert_approved_totals(projected_rows,{"totale_entrate_mappate":args.approved_income,"totale_uscite_mappate":args.approved_expense})
    if money(approved_totals["surplus"])!=money(args.approved_surplus) or money(bridge["closing"])!=money(args.approved_closing):
        raise ValueError("APPROVED_TOTALS_CHANGED")
    approved_totals["approved_closing"]=args.approved_closing
    # Detect exact source replay without choosing a new owner or mutating any row.
    imports = [dict(r) for r in connection.execute("SELECT import_id,account_id,hash_file,source_type,import_timestamp FROM imports")]
    source_inputs = []
    for imp in imports:
        imported = [r for r in records if r["import_id"]==imp["import_id"]]
        if not imported or not imp["hash_file"]:continue
        raw = [json.loads(r[0]) for r in connection.execute("SELECT payload_originale_json FROM movements_raw WHERE import_id=? ORDER BY row_number",(imp["import_id"],))]
        source_inputs.append({"import_id":imp["import_id"],"account_id":imp["account_id"],"source_hash":imp["hash_file"],"imported_at":imp["import_timestamp"],"raw_rows":raw,"ledger_rows":[(r["data_movimento"],r["importo_signed"]) for r in imported]})
    duplicate_sources = duplicate_groups(source_inputs)
    duplicate_import_ids={i for group in duplicate_sources for i in group["import_ids"]}
    reconciled_accounts = []
    for identity, account in accounts.items():
        if not account.get("is_operational_for_association"):continue
        rows = [r for r in records if r["account_id"]==identity and r["import_id"] not in duplicate_import_ids]
        if not rows and any(identity in g["account_ids"] for g in duplicate_sources):
            reconciled_accounts.append({"account_id":identity,"status":"SOURCE_OWNER_UNVERIFIED","opening":None,"closing":None,"residual":None})
            continue
        statements = [s for s in savings if any(i["import_id"]==s["import_id"] and i["account_id"]==identity for i in imports)]
        compatible = [s for s in statements if len(rows)==sum(r["import_id"]==s["import_id"] for r in rows)]
        evidence = compatible[0] if len(compatible)==1 else {}
        reconciled_accounts.append(reconcile_account(identity,opening=evidence.get("opening"),movements=rows,closing=evidence.get("closing")))
    # One owner-neutral source unit, not two account copies. No ownership inferred.
    for group in duplicate_sources:
        evidence=next((s for s in savings if s["import_id"] in group["import_ids"]),None)
        if evidence:
            result=reconcile_account(group["group_id"],opening=evidence["opening"],movements=[{"account_id":group["group_id"],"importo_signed":group["projected_source_delta"]}],closing=evidence["closing"])
            reconciled_accounts.append({**result,"instrument":"libretto","owner":group["preferred_owner"],"source_hash":group["source_hash"],"source_occurrences":1})
    report = {"year":args.year, "summary":draft["summary"], "bridge":bridge,
        "cash_unadjusted":str(cash), "bank_unadjusted":str(bank),
        "savings_statement_evidence":savings,
        "excluded_items":excluded, "unmapped_items":unmapped, "personal_advance_items":[{k:r[k] for k in ("movement_id","account_id","importo_signed","movement_kind")} for r in personal],
        "existing_decisions":decisions, "reimbursement_links":reimbursements,
        "projected_management_rows":projected_rows,
        "approved_totals":approved_totals,
        "duplicate_financial_sources":duplicate_sources,"account_reconciliation":reconciled_accounts,
        "legacy_unmapped_resolution":[decision_by_id[r["movement_id"]] for r in unmapped],
        "existing_decisions_preserved":True,"financial_reconciliation_complete":False, "writes":0,
        "snapshot_sha256":hashlib.sha256(snapshot_row[0].encode()).hexdigest(),
        "movement_input_sha256":hashlib.sha256(json.dumps(records,sort_keys=True).encode()).hexdigest(),
        "report_code_sha256":hashlib.sha256((args.suite / "app/api/runts_report.py").read_bytes()).hexdigest()}
    report_bytes = json.dumps(report, sort_keys=True, indent=2).encode()
    (args.output / "reconciliation.json").write_bytes(report_bytes)

    overlay = root / "integrations/runts_suite"
    spec = importlib.util.spec_from_file_location("review_modd_builder", overlay / "app/services/runts_modd_builder.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    built = module.build_modd_matrix(projected_rows, previous["rows"])
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
    blockers = ["CAPITAL_AND_TAX_PRESENTATION_REVIEW_REQUIRED"]
    if any(decision_by_id[r["movement_id"]]["classification_status"]=="UNRESOLVED_CLASSIFICATION" for r in unmapped):blockers.append("UNRESOLVED_CLASSIFICATION")
    if any(d["classification_status"]=="MAPPING_MISSING" for d in projection_decisions if d["financial_account"]["entity_operational"] or d["advance_type"]=="member_advance"):blockers.append("EXISTING_DECISION_MAPPING_MISSING")
    blockers.extend(sorted({r["status"] for r in reimbursements if r["status"]!="VERIFIED_LINK"}))
    if any(g["status"]=="BLOCKED_REVIEW" for g in duplicate_sources):blockers.append("DUPLICATE_SOURCE_OWNER_UNVERIFIED")
    if any(r["status"]!="RECONCILED" for r in reconciled_accounts):blockers.append("ACCOUNT_BALANCE_EVIDENCE_REQUIRED")
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
    print(json.dumps({"status":proposal.status,"bridge":bridge,"approved_totals":approved_totals,"pdf_sha256":document.content_hash,"corrections":built["corrections"],"blockers":blockers,"writes":0}))


if __name__ == "__main__":
    main()
