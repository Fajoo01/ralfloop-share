from __future__ import annotations

from contextlib import AbstractContextManager
from datetime import date
from decimal import Decimal
from email.utils import parsedate_to_datetime
import hashlib
import json
import re
from pathlib import Path
from typing import Any, Callable, Mapping

from .accounting import parse_eur
from .email_search import GoogleWorkspaceEmailSearch


_PAYPAL_AMOUNT_MERCHANT_RE = re.compile(
    r"Hai\s+pagato\s+(?P<amount>\d{1,3}(?:[.\s]\d{3})*(?:,\d{2})?|\d+(?:[.,]\d{2})?)\s*€(?:\s*EUR)?\s+a\s+(?P<merchant>[^\n\r<]{2,160})",
    re.I,
)
_PAYPAL_TX_RE = re.compile(r"codice\s+transazione:\s*([A-Z0-9]{8,32})", re.I)
_PAYPAL_CARD_RE = re.compile(r"ultime\s+cifre\s+sono\s+(\d{4})", re.I)
_TOKEN_RE = re.compile(r"[a-z0-9]+", re.I)
_BANK_CRO_RE = re.compile(r"\bCro:\s*([A-Z0-9]{8,80})", re.I)
_BANK_BILL_RE = re.compile(r"\b(?:Bolletta\s+Nr\.?|N\.DOCUMENTO)\s*[:.]?\s*([A-Z0-9-]{6,80})", re.I)
_BANK_DIRECT_DEBIT_RE = re.compile(r"\bADDEBITO\s+DIRETTO\s+CORE\b", re.I)
_BANK_SDD_UTILITY_RE = re.compile(r"\bPAGAMENTO\s+UTENZA(?:\s+TELEFONICA)?\b.*\b(?:CORE\s+RCUR|SDD)\b", re.I)
_BANK_CARD_MERCHANT_RE = re.compile(r"\bPagamenti\s+paesi\s+UE\s+DEL\s+\d{2}/\d{2}/\d{2}\b.*\bC/O\s+.+?\s+CARTA\s+N\.", re.I)
_BANK_F24_RE = re.compile(r"\b(?:DELEGA\s+F24|ADD\.DELEGA\s+F24)\b", re.I)
_BANK_CBILL_RE = re.compile(r"\bCBILL\b", re.I)
_BANK_FEE_RE = re.compile(r"^\s*(?:COMPETENZE\s+SPESE|COMMISSIONE|COMMISSIONI(?:\s+BANCARIE)?|CANONE\s+CONTO)\s*$", re.I)
_BANK_WITHHOLDING_RE = re.compile(r"^\s*RITENUT[AE]\s+SU\s+INTERESSI\s*$", re.I)
_BANK_TRANSFER_RE = re.compile(r"\b(?:ADDEBITO\s+BONIFICO|DISPOSIZIONE\s+DI\s+BONIFICO|Bonifico\s+Disposto)\b", re.I)
_BANK_PURPOSE_RE = re.compile(r"\b(?:FATTURA|RICEVUTA|NOTA|RIMBORSO|AFFITTO|PRESTITO|TESSER|SUPPORTO|INTERVALLO|LEZION|COMPENSO)\w*\b", re.I)


def _q2(value: Any) -> Decimal:
    return parse_eur(value).quantize(Decimal("0.01"))


def _message_date(value: str) -> str | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        return parsedate_to_datetime(raw).date().isoformat()
    except Exception:
        try:
            return date.fromisoformat(raw[:10]).isoformat()
        except Exception:
            return None


def _merchant_tokens(value: str) -> tuple[str, ...]:
    stop = {"it", "com", "www", "srl", "spa", "eu", "the", "di", "la", "il"}
    tokens = []
    for token in _TOKEN_RE.findall(str(value or "").casefold()):
        if len(token) < 3 or token in stop:
            continue
        tokens.append(token)
    return tuple(dict.fromkeys(tokens))


def merchant_similarity(left: str, right: str) -> float:
    a = set(_merchant_tokens(left))
    b = set(_merchant_tokens(right))
    if not a or not b:
        return 0.0
    if a <= b or b <= a:
        return 1.0
    return len(a & b) / len(a | b)


def extract_bank_native_reference(movement: Mapping[str, Any]) -> dict[str, Any] | None:
    description = str(movement.get("description") or movement.get("descrizione_originale") or "").strip()
    movement_id = str(movement.get("movement_id") or "").strip()
    if not description or not movement_id:
        return None
    kind = None
    confidence = 0
    document_reference = None
    if _BANK_F24_RE.search(description):
        kind, confidence = "bank_f24_reference", 95
    elif _BANK_CBILL_RE.search(description):
        kind, confidence = "bank_cbill_reference", 92
        match = _BANK_BILL_RE.search(description)
        document_reference = match.group(1) if match else None
    elif _BANK_DIRECT_DEBIT_RE.search(description):
        kind, confidence = "bank_direct_debit_reference", 90
        match = _BANK_BILL_RE.search(description)
        document_reference = match.group(1) if match else None
    elif _BANK_SDD_UTILITY_RE.search(description):
        kind, confidence = "bank_sdd_utility_reference", 92
    elif _BANK_CARD_MERCHANT_RE.search(description) and "PAYPAL *ADD TO BAL" not in description.upper():
        kind, confidence = "bank_card_merchant_reference", 90
    elif _BANK_FEE_RE.fullmatch(description):
        kind, confidence = "bank_fee_statement_reference", 95
    elif _BANK_WITHHOLDING_RE.fullmatch(description):
        kind, confidence = "bank_interest_withholding_reference", 95
    elif _BANK_TRANSFER_RE.search(description):
        cro = _BANK_CRO_RE.search(description)
        purpose = _BANK_PURPOSE_RE.search(description)
        if cro and purpose:
            kind, confidence = "bank_transfer_reference", 92
        elif cro:
            kind, confidence = "bank_transfer_reference", 82
        else:
            return None
    else:
        return None
    native = _BANK_CRO_RE.search(description)
    native_reference = native.group(1) if native else document_reference
    fingerprint = hashlib.sha256(description.encode("utf-8")).hexdigest()
    return {
        "kind": kind,
        "confidence": confidence,
        "provenance_ref": f"runts:movement:{movement_id}:bank_native_reference",
        "native_reference_hash": hashlib.sha256(str(native_reference or fingerprint).encode("utf-8")).hexdigest(),
        "document_reference": document_reference,
        "content_hash": fingerprint,
        "fiscal_document": False,
        "content_role": "payment_and_context_evidence",
    }


def enrich_rows_with_bank_native(rows: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for raw in rows:
        row = dict(raw)
        evidence = extract_bank_native_reference(row)
        existing = list(row.get("external_evidence") or ())
        if evidence is not None:
            existing.append(evidence)
            row["bank_native_match_status"] = "UNIQUE_MOVEMENT_REFERENCE"
            if int(evidence["confidence"]) >= 90:
                row["ready_for_human_confirmation"] = True
            row["reconstruction_evidence_score"] = max(
                int(row.get("reconstruction_evidence_score") or 0), int(evidence["confidence"])
            )
        row["external_evidence"] = existing
        output.append(row)
    return output


def parse_paypal_receipt(message: Mapping[str, Any]) -> dict[str, Any] | None:
    body = str(message.get("body") or "")
    match = _PAYPAL_AMOUNT_MERCHANT_RE.search(body)
    if not match:
        return None
    amount = _q2(match.group("amount"))
    tx = _PAYPAL_TX_RE.search(body)
    card = _PAYPAL_CARD_RE.search(body)
    message_id = str(message.get("messageId") or message.get("message_id") or "").strip()
    if not message_id:
        return None
    merchant = " ".join(match.group("merchant").replace("\xa0", " ").split())[:160]
    observed_date = _message_date(str(message.get("date") or ""))
    content_hash = hashlib.sha256(
        json.dumps(
            {
                "message_id": message_id,
                "date": observed_date,
                "amount": str(amount),
                "merchant": merchant,
                "transaction_id": tx.group(1).upper() if tx else None,
                "card_suffix": card.group(1) if card else None,
            },
            sort_keys=True,
            ensure_ascii=False,
        ).encode("utf-8")
    ).hexdigest()
    return {
        "kind": "paypal_email_receipt",
        "message_id": message_id,
        "date": observed_date,
        "amount_eur": str(amount),
        "merchant": merchant,
        "transaction_id": tx.group(1).upper() if tx else None,
        "card_suffix": card.group(1) if card else None,
        "sender": str(message.get("from") or message.get("sender") or "")[:320],
        "subject": str(message.get("subject") or "")[:500],
        "provenance_ref": f"gmail:message:{message_id}",
        "content_hash": content_hash,
        "content_role": "payment_evidence",
        "fiscal_document": False,
    }


def collect_paypal_receipts(
    *,
    year: int,
    context_factory: Callable[[], AbstractContextManager] | None = None,
    monthly_limit: int = 50,
) -> dict[str, Any]:
    if year < 2000 or year > 2100:
        raise ValueError("invalid_year")
    limit = max(1, min(int(monthly_limit), 50))
    factory = context_factory or GoogleWorkspaceEmailSearch.from_environment().gateway_factory
    seen: set[str] = set()
    receipts: list[dict[str, Any]] = []
    search_rows: list[dict[str, Any]] = []
    with factory() as gateway:
        for month in range(1, 13):
            after = date(year, month, 1)
            before = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
            query = (
                'from:(assistenza@paypal.it) '
                'subject:"Ricevuta della tua transazione recente" '
                f'after:{after.strftime("%Y/%m/%d")} before:{before.strftime("%Y/%m/%d")}'
            )
            result = gateway.invoke("search", query=query, maxResults=limit)
            messages = result.get("messages") if isinstance(result, Mapping) else None
            candidates = messages if isinstance(messages, list) else []
            if len(candidates) >= limit:
                raise RuntimeError(f"paypal_receipt_month_cap_reached:{after.isoformat()}")
            search_rows.append({
                "month": after.strftime("%Y-%m"),
                "query": query,
                "results": len(candidates),
                "exhausted": True,
            })
            for summary in candidates:
                if not isinstance(summary, Mapping):
                    continue
                message_id = str(summary.get("messageId") or summary.get("message_id") or summary.get("id") or "").strip()
                if not message_id or message_id in seen:
                    continue
                detail = gateway.invoke("read", messageId=message_id)
                message = detail.get("message") if isinstance(detail, Mapping) else None
                if not isinstance(message, Mapping):
                    continue
                parsed = parse_paypal_receipt(message)
                if parsed is None:
                    continue
                seen.add(message_id)
                receipts.append(parsed)
    receipts.sort(key=lambda row: (str(row.get("date") or ""), row["message_id"]))
    return {
        "schema_version": 1,
        "kind": "accounting_external_evidence_snapshot",
        "source": "gmail_paypal_receipts_read_only",
        "year": year,
        "receipt_count": len(receipts),
        "searches": search_rows,
        "receipts": receipts,
        "writes": 0,
        "sends": 0,
    }


def save_evidence_snapshot(snapshot: Mapping[str, Any], path: str | Path) -> Path:
    target = Path(path).expanduser()
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(snapshot, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    target.write_text(payload, encoding="utf-8")
    return target


def load_evidence_snapshot(path: str | Path) -> dict[str, Any]:
    source = Path(path).expanduser().resolve()
    raw = source.read_bytes()
    payload = json.loads(raw.decode("utf-8"))
    if payload.get("schema_version") != 1 or not isinstance(payload.get("receipts"), list):
        raise ValueError("accounting_evidence_snapshot_invalid")
    payload["_snapshot_sha256"] = hashlib.sha256(raw).hexdigest()
    payload["_snapshot_path"] = str(source)
    return payload


def match_paypal_receipt(
    movement: Mapping[str, Any], receipts: list[Mapping[str, Any]], *, max_days: int = 4
) -> dict[str, Any] | None:
    amount = _q2(movement.get("amount_eur"))
    movement_date_raw = str(movement.get("movement_date") or "")
    try:
        movement_date = date.fromisoformat(movement_date_raw)
    except ValueError:
        return None
    merchant_text = " ".join(
        str(value or "")
        for value in (movement.get("counterparty"), movement.get("description"))
    )
    candidates: list[tuple[int, float, Mapping[str, Any]]] = []
    for receipt in receipts:
        try:
            if _q2(receipt.get("amount_eur")) != amount:
                continue
            receipt_date = date.fromisoformat(str(receipt.get("date") or ""))
        except Exception:
            continue
        days = abs((movement_date - receipt_date).days)
        if days > max_days:
            continue
        similarity = merchant_similarity(merchant_text, str(receipt.get("merchant") or ""))
        if similarity < 0.34:
            continue
        candidates.append((days, similarity, receipt))
    if not candidates:
        return None
    candidates.sort(key=lambda item: (item[0], -item[1], str(item[2].get("message_id") or "")))
    best = candidates[0]
    tied = [row for row in candidates if row[0] == best[0] and abs(row[1] - best[1]) < 1e-9]
    if len(tied) != 1:
        return {
            "status": "AMBIGUOUS",
            "candidate_count": len(candidates),
            "amount_eur": str(amount),
        }
    receipt = dict(best[2])
    confidence = 95 if best[0] <= 2 and best[1] >= 0.5 else 85
    return {
        "status": "UNIQUE_MATCH",
        "confidence": confidence,
        "date_distance_days": best[0],
        "merchant_similarity": round(best[1], 4),
        "receipt": receipt,
        "evidence_refs": [str(receipt.get("provenance_ref") or "")],
        "fiscal_document": False,
    }


def enforce_unique_external_evidence(rows: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Fail closed when the same external evidence would justify more than one movement."""
    output = [dict(row) for row in rows]
    owners: dict[str, list[int]] = {}
    for index, row in enumerate(output):
        for ev in row.get("external_evidence") or ():
            ref = str(ev.get("provenance_ref") or "").strip()
            if ref:
                owners.setdefault(ref, []).append(index)
    conflicted = {ref for ref, indexes in owners.items() if len(set(indexes)) > 1}
    if not conflicted:
        return output
    for row in output:
        refs = {
            str(ev.get("provenance_ref") or "").strip()
            for ev in row.get("external_evidence") or ()
        }
        overlap = sorted(refs & conflicted)
        if overlap:
            row["ready_for_human_confirmation"] = False
            row["external_evidence_conflict"] = overlap
            row["reconstruction_evidence_score"] = min(
                int(row.get("reconstruction_evidence_score") or 0), 60
            )
    return output


def enrich_rows_with_paypal(
    rows: list[Mapping[str, Any]], snapshot: Mapping[str, Any]
) -> list[dict[str, Any]]:
    receipts = [row for row in snapshot.get("receipts") or () if isinstance(row, Mapping)]
    output: list[dict[str, Any]] = []
    for raw in rows:
        row = dict(raw)
        match = match_paypal_receipt(row, receipts)
        existing = list(row.get("external_evidence") or ())
        if match and match.get("status") == "UNIQUE_MATCH":
            existing.append({
                "kind": "paypal_email_receipt",
                "confidence": match["confidence"],
                "provenance_ref": match["receipt"]["provenance_ref"],
                "content_hash": match["receipt"].get("content_hash"),
                "merchant": match["receipt"].get("merchant"),
                "amount_eur": match["receipt"].get("amount_eur"),
                "date": match["receipt"].get("date"),
                "transaction_id": match["receipt"].get("transaction_id"),
                "card_suffix": match["receipt"].get("card_suffix"),
                "fiscal_document": False,
            })
            row["ready_for_human_confirmation"] = True
            row["reconstruction_evidence_score"] = max(
                int(row.get("reconstruction_evidence_score") or 0), int(match["confidence"])
            )
            row["paypal_match_status"] = "UNIQUE_MATCH"
        elif match:
            row["paypal_match_status"] = str(match.get("status"))
        row["external_evidence"] = existing
        output.append(row)
    return output


__all__ = [
    "collect_paypal_receipts",
    "enforce_unique_external_evidence",
    "enrich_rows_with_bank_native",
    "enrich_rows_with_paypal",
    "extract_bank_native_reference",
    "load_evidence_snapshot",
    "match_paypal_receipt",
    "merchant_similarity",
    "parse_paypal_receipt",
    "save_evidence_snapshot",
]
