"""Bounded source-backed financial projections. Never mutates source rows."""
from collections import defaultdict
from datetime import date
from decimal import Decimal
from itertools import combinations
import re
from .runts_accounting import version


def duplicate_groups(imports, *, owner_evidence=()):
    groups = defaultdict(list)
    for item in imports:
        if item.get("source_hash") and item.get("raw_rows") and item.get("ledger_rows"):
            key=(item["source_hash"],version(item["raw_rows"]),version(item["ledger_rows"]))
            groups[key].append(item)
    output=[]
    for key, rows in groups.items():
        if len(rows)<2:continue
        rows=sorted(rows,key=lambda r:(r["imported_at"],r["import_id"]))
        proven={e["account_id"] for e in owner_evidence if e.get("source_hash")==key[0]
            and e.get("source_ref") and e.get("mapping_ref")
            and re.fullmatch(r"[a-f0-9]{64}", e.get("source_instrument_hash", ""))
            and e["source_instrument_hash"] == e.get("account_instrument_hash")
            and e["account_id"] in {r["account_id"] for r in rows}}
        owner=next(iter(proven)) if len(proven)==1 else None
        output.append({"group_id":"duplicate."+version(key)[:24],"import_ids":[r["import_id"] for r in rows],
            "account_ids":[r["account_id"] for r in rows],"original_import_id":rows[0]["import_id"],
            "replay_import_ids":[r["import_id"] for r in rows[1:]],"source_hash":key[0],"raw_fingerprint":key[1],
            "ledger_fingerprint":key[2],"row_count":len(rows[0]["ledger_rows"]),
            "preferred_owner":owner,"owner_evidence":[e for e in owner_evidence if e.get("source_hash")==key[0]],
            "status":"VERIFIED_OWNER" if owner is not None else "BLOCKED_REVIEW",
            # A source is represented once even when account ownership is unresolved.
            "projected_source_delta":str(sum((Decimal(str(r[1])) for r in rows[0]["ledger_rows"]),Decimal(0))),
            "projection_occurrences":1})
    return output


def reconcile_statement(rows, *, source_ref):
    """Reconcile one ordered source, including every intermediate balance.

    Input amounts are canonical decimal strings, not locale-formatted numbers.
    This proves the instrument balance, NOT its ownership by a ledger account.
    """
    result = {"opening":None,"movement_delta":None,"closing":None,"residual":None,
              "evidence_sources":[source_ref] if source_ref else [],
              "status":"MISSING_BALANCE_EVIDENCE","row_count":len(rows)}
    if not rows or not source_ref or any(r.get("balance") is None or r.get("amount") is None for r in rows):
        return result
    amounts = [Decimal(str(r["amount"])) for r in rows]
    balances = [Decimal(str(r["balance"])) for r in rows]
    if not all(x.is_finite() for x in (*amounts,*balances)):
        raise ValueError("statement_amount_invalid")
    opening = balances[0] - amounts[0]
    running = opening
    conflicts = []
    for index,(amount,balance) in enumerate(zip(amounts,balances)):
        running += amount
        if running != balance:conflicts.append(index)
    delta = sum(amounts,Decimal(0))
    return {**result,"opening":str(opening),"movement_delta":str(delta),
            "closing":str(balances[-1]),"residual":str(running-balances[-1]),
            "conflicting_row_indexes":conflicts,
            "opening_derivation":"first_source_balance_minus_first_source_movement",
            "status":"ACCOUNT_BALANCE_CONFLICT" if conflicts else "RECONCILED"}


def match_reimbursement(advances, movements, accounts, *, reimbursement_date, window_days=7, max_parts=2):
    """Require exact transfer identity or reciprocal native account references.

    Amount/date alone never verifies a repayment. Wrong note IDs are not used.
    Bound search size, and never choose the first of several possible pairings.
    """
    if max_parts not in {1,2} or not 0<=window_days<=31:
        raise ValueError("reimbursement_search_bound_invalid")
    target=date.fromisoformat(reimbursement_date)
    expected=sum((-Decimal(str(m["importo_signed"])) for m in advances),Decimal(0))
    personal={m["account_id"] for m in advances}
    eligible=[m for m in movements if m.get("source_ref") and abs((date.fromisoformat(m["data_movimento"])-target).days)<=window_days]
    if len(eligible)>500:return {"status":"AMBIGUOUS_REIMBURSEMENT_LINK","reason":"candidate_safety_bound","pairings":[]}
    outgoing=[m for m in eligible if accounts[m["account_id"]].get("is_operational_for_association") and Decimal(str(m["importo_signed"]))<0]
    incoming=[m for m in eligible if m["account_id"] in personal and Decimal(str(m["importo_signed"]))>0]
    pairs=[]
    for a in outgoing:
        for b in incoming:
            if Decimal(str(a["importo_signed"])) != -Decimal(str(b["importo_signed"])):continue
            if abs((date.fromisoformat(a["data_movimento"])-date.fromisoformat(b["data_movimento"])).days)>3:continue
            exact_ref=bool(a.get("transfer_reference") and a["transfer_reference"]==b.get("transfer_reference"))
            reciprocal=a.get("counterparty_account_id")==b["account_id"] and b.get("counterparty_account_id")==a["account_id"]
            if exact_ref or reciprocal:
                pairs.append((a,b))
    if len(pairs)>40:return {"status":"AMBIGUOUS_REIMBURSEMENT_LINK","reason":"candidate_safety_bound","pairings":[]}
    matches=[]
    for count in range(1,max_parts+1):
        for group in combinations(pairs,count):
            ids=[r["movement_id"] for pair in group for r in pair]
            if len(set(ids))!=len(ids):continue
            if sum((-Decimal(str(a["importo_signed"])) for a,b in group),Decimal(0))==expected:
                matches.append({"outgoing_ids":[a["movement_id"] for a,b in group],"incoming_ids":[b["movement_id"] for a,b in group],"sources":[r["source_ref"] for pair in group for r in pair]})
    status="VERIFIED_LINK" if len(matches)==1 else "AMBIGUOUS_REIMBURSEMENT_LINK" if matches else "REIMBURSEMENT_LINK_UNVERIFIED"
    return {"status":status,"expected_amount":str(expected),"date_window_days":window_days,"pairings":matches,"candidate_pair_count":len(pairs)}


def assert_approved_totals(projected_rows, approved_summary):
    actual={side:sum((Decimal(str(r["totale"])) for r in projected_rows if r["side"]==side),Decimal(0)) for side in ("entrata","uscita")}
    if actual["entrata"]!=Decimal(str(approved_summary["totale_entrate_mappate"])) or actual["uscita"]!=Decimal(str(approved_summary["totale_uscite_mappate"])):
        raise ValueError("APPROVED_TOTALS_CHANGED")
    return {"status":"PRESERVED","income":str(actual["entrata"]),"expense":str(actual["uscita"]),"surplus":str(actual["entrata"]-actual["uscita"])}
