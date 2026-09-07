"""Read-only documentary audit. Source identity never follows import chronology."""
import hashlib
import csv
import io
import json
import re
import sqlite3
import subprocess
from decimal import Decimal

from .runts_financial import reconcile_statement


def financial_source_audit(connection, suite, year, accounts, decisions):
    imports = [dict(r) for r in connection.execute("SELECT * FROM imports")]
    source_reports = []
    identifiers = set()
    for imp in imports:
        if not imp["hash_file"]:
            continue
        rows = [json.loads(r[0]) for r in connection.execute(
            "SELECT payload_originale_json FROM movements_raw WHERE import_id=? ORDER BY row_number,raw_id",
            (imp["import_id"],))]
        ledger = [Decimal(str(r[0])) for r in connection.execute(
            "SELECT importo_signed FROM movements WHERE import_id=? AND data_movimento>=? AND data_movimento<?",
            (imp["import_id"],f"{year}-01-01",f"{year+1}-01-01"))]
        if not ledger:continue
        source = {"import_id":imp["import_id"],"source_hash":imp["hash_file"]}
        statement = []
        instrument = "UNVERIFIED"
        if imp["source_type"] in {"coop_libretto_pdf","coop_libretto_text"}:
            instrument = "savings"
            statement = [{"amount":r.get("importo"),"balance":r.get("saldo_contabile")}
                         for r in rows if str(r.get("data", "")).startswith(str(year))]
        elif rows and {"Netto","Saldo","Impatto sul saldo"}.issubset(rows[0]):
            instrument = "wallet"
            def amount(value):
                return None if value in (None,"") else str(Decimal(str(value).replace(".","").replace(",",".")))
            statement = [{"amount":amount(r.get("Netto")),"balance":amount(r.get("Saldo"))}
                         for r in rows if str(r.get("Data", "")).endswith("/"+str(year))]
        elif imp["source_type"] == "xlsx_bank_italia":
            instrument = "bank"
        path = suite / "data/uploads" / imp["filename"]
        # Filename is a DB value; never let it escape the configured upload root.
        inside = path.resolve().is_relative_to((suite / "data/uploads").resolve())
        exists = inside and path.is_file()
        verified = exists and hashlib.sha256(path.read_bytes()).hexdigest() == imp["hash_file"]
        raw_verified = None
        if verified and instrument == "wallet":
            try:
                raw_verified = list(csv.DictReader(io.StringIO(path.read_text(encoding="utf-8-sig")))) == rows
            except (UnicodeError,csv.Error):
                raw_verified = False
        account_matches = []
        if verified and imp["source_type"] == "coop_libretto_pdf":
            text = subprocess.run(["pdftotext","-layout",str(path),"-"],capture_output=True,check=True).stdout.decode()
            # Only compare identifier tokens from the header, never serialize them.
            tokens = set(re.findall(r"\b\d{6,}\b", "\n".join(text.splitlines()[:15])))
            identifiers.update(tokens)
            for identity,account in accounts.items():
                alias = re.sub(r"\W","",account.get("iban_o_alias") or "")
                if alias in tokens:account_matches.append(identity)
        result = reconcile_statement(statement,source_ref=source)
        if not verified:
            result["status"] = "MISSING_BALANCE_EVIDENCE"
        elif raw_verified is False:
            result["status"] = "ACCOUNT_BALANCE_CONFLICT"
        source_reports.append({**source,"assigned_account_id":imp["account_id"],
            "instrument":instrument,"source_file_verified":verified,
            "source_raw_rows_verified":raw_verified,
            "ledger_row_count":len(ledger),"ledger_delta":str(sum(ledger,Decimal(0))),
            "exact_header_account_matches":account_matches,"statement":result,
            "ownership_status":"OWNERSHIP_UNVERIFIED"})
    snapshots = []
    for path in sorted(suite.glob("runts_suite.db.bak*")):
        try:
            with sqlite3.connect(path.resolve().as_uri()+"?mode=ro",uri=True) as old:
                matches = [r[0] for r in old.execute("SELECT account_id,iban_o_alias FROM accounts")
                           if re.sub(r"\W","",r[1] or "") in identifiers]
            snapshots.append({"source":path.name,"sha256":hashlib.sha256(path.read_bytes()).hexdigest(),
                              "exact_instrument_account_matches":matches})
        except sqlite3.Error:
            snapshots.append({"source":path.name,"status":"UNREADABLE_SNAPSHOT"})
    scopes = []
    for instrument,types in (("bank",{"banca"}),("cash",{"cassa","cash","contanti"}),("wallet",{"wallet"})):
        ids = [i for i,a in accounts.items() if a.get("is_operational_for_association") and a["tipo_account"].lower() in types]
        sources = [r for r in source_reports if r["instrument"]==instrument]
        scopes.append({"account":instrument,"account_ids":ids,"opening":None,
            "movement_delta":None,"closing":None,"residual":None,
            "source_movement_delta":str(sum((Decimal(r["ledger_delta"]) for r in sources),Decimal(0))) if sources else None,
            "evidence_sources":[{"import_id":r["import_id"],"source_hash":r["source_hash"]} for r in sources],
            "status":"MISSING_BALANCE_EVIDENCE",
            "reason":"no_cash_ledger_or_independent_closing" if not ids else "independent_account_balances_and_source_ownership_required"})
    relevant = []
    for d in decisions:
        if not (d["financial_account"]["entity_operational"] or d["financial_account"]["requires_reimbursement"]):continue
        classification = d.get("existing_accounting_classification") or ""
        if not re.search(r"TASS|IMPOST|INVEST|FINANZI|PRESTIT",classification):continue
        source_row = connection.execute("SELECT m.import_id,m.data_movimento,m.descrizione_originale,i.hash_file FROM movements m JOIN imports i USING(import_id) WHERE m.movement_id=?",(d["movement_id"],)).fetchone()
        relevant.append({"movement_id":d["movement_id"],"existing_classification":classification,
            "existing_mapping":d["existing_runts_mapping"],"amount":d["signed_amount"],
            "evidence":{"import_id":source_row[0],"date":source_row[1],"source_hash":source_row[3],
                        "f24_reference_present":bool(re.search(r"\bF24\b",source_row[2] or "",re.I))} if source_row else None,
            "decision_version":d["decision_version"],"presentation_decision":"PRESERVE_EXISTING_PENDING_DOCUMENTARY_REVIEW"})
    return {"sources":source_reports,"ownership_snapshots":snapshots,"account_scopes":scopes,
        "capital_tax_candidates":relevant,
        "capital_tax_status":"CAPITAL_AND_TAX_PRESENTATION_REVIEW_REQUIRED",
        "zero_sections_authorized":False,"writes":0}
