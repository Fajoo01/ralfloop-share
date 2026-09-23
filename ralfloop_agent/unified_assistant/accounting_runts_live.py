from __future__ import annotations

from contextlib import contextmanager
from datetime import date
from decimal import Decimal
from pathlib import Path
import hashlib
import json
import re
import sqlite3
from typing import Any, Iterator, Mapping

from .accounting import parse_eur
from .accounting_review import review_missing_document_case


def _q2(value: Any) -> Decimal:
    return parse_eur(value).quantize(Decimal("0.01"))


@contextmanager
def open_runts_readonly(db_path: str | Path) -> Iterator[sqlite3.Connection]:
    path = Path(db_path).expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(f"runts_database_missing:{path}")
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    try:
        yield conn
    finally:
        conn.close()


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone()
    return row is not None


def _expense_rows(conn: sqlite3.Connection, year: int) -> list[sqlite3.Row]:
    if year < 2000 or year > 2100:
        raise ValueError("invalid_year")
    return conn.execute(
        """
        SELECT
            m.movement_id,
            m.import_id,
            i.filename AS import_filename,
            i.source_type AS import_source_type,
            i.parser_usato AS import_parser,
            i.hash_file AS import_hash,
            i.import_timestamp,
            ROW_NUMBER() OVER (PARTITION BY m.import_id ORDER BY m.movement_id) AS import_row_ordinal,
            m.account_id,
            m.data_movimento,
            m.descrizione_originale,
            m.importo_signed,
            m.contropartita,
            m.category_id,
            m.project_id,
            m.internal_category_id,
            m.movement_kind,
            m.notes,
            m.stato_validazione,
            c.nome_categoria AS category_name,
            c.tipo AS category_type,
            a.nome_account AS account_name,
            a.is_operational_for_association,
            a.requires_reimbursement,
            mr.decisione AS review_decision,
            mr.runts_excluded,
            mr.runts_detached,
            mr.runts_category_id_override,
            mr.category_id_manuale,
            mr.internal_category_id_manuale,
            mr.project_id_manuale,
            mr.note_revisione
        FROM movements m
        JOIN imports i ON i.import_id = m.import_id
        JOIN accounts a ON a.account_id = m.account_id
        LEFT JOIN categories c ON c.category_id = m.category_id
        LEFT JOIN movement_reviews mr ON mr.movement_id = m.movement_id
        WHERE m.data_movimento >= ?
          AND m.data_movimento < ?
          AND m.importo_signed < 0
          AND COALESCE(m.is_transfer, 0) = 0
          AND COALESCE(m.is_duplicate, 0) = 0
          AND (
                COALESCE(a.is_operational_for_association, 1) = 1
                OR (
                    COALESCE(a.requires_reimbursement, 0) = 1
                    AND m.movement_kind = 'member_advance'
                )
          )
          AND COALESCE(mr.runts_excluded, 0) = 0
          AND COALESCE(mr.runts_detached, 0) = 0
          AND COALESCE(mr.decisione, '') <> 'escludi_runts'
          AND COALESCE(c.tipo, '') NOT IN ('trasferimento', 'escluso')
        ORDER BY ABS(m.importo_signed) DESC, m.data_movimento ASC, m.movement_id ASC
        """,
        (f"{year}-01-01", f"{year + 1}-01-01"),
    ).fetchall()


def _attach_raw_source_rows(
    conn: sqlite3.Connection, rows: list[sqlite3.Row]
) -> list[dict[str, Any]]:
    output = [dict(row) for row in rows]
    for row in output:
        row["raw_source_id"] = None
        row["raw_source_row_number"] = None
        row["raw_payload_json"] = None
    if not output or not _table_exists(conn, "movements_raw"):
        return output
    import_ids = sorted({int(row["import_id"]) for row in output})
    placeholders = ",".join("?" for _ in import_ids)
    ordinal_by_key: dict[tuple[int, int], int] = {}
    counters: dict[int, int] = {}
    for source in conn.execute(
        f"SELECT movement_id, import_id FROM movements WHERE import_id IN ({placeholders}) ORDER BY import_id, movement_id",
        tuple(import_ids),
    ):
        import_id = int(source["import_id"])
        ordinal = counters.get(import_id, 0) + 1
        counters[import_id] = ordinal
        ordinal_by_key[(import_id, int(source["movement_id"]))] = ordinal
    raw_by_key = {
        (int(raw["import_id"]), int(raw["row_number"])): raw
        for raw in conn.execute(
            f"SELECT raw_id, import_id, row_number, payload_originale_json FROM movements_raw WHERE import_id IN ({placeholders})",
            tuple(import_ids),
        )
    }
    for row in output:
        key = (int(row["import_id"]), int(row["movement_id"]))
        ordinal = ordinal_by_key.get(key)
        row["raw_source_row_number"] = ordinal
        raw = raw_by_key.get((key[0], int(ordinal))) if ordinal is not None else None
        if raw is not None:
            row["raw_source_id"] = int(raw["raw_id"])
            row["raw_payload_json"] = str(raw["payload_originale_json"] or "")
    return output


def _raw_payload(row: Mapping[str, Any]) -> dict[str, Any]:
    raw = row.get("raw_payload_json")
    if not raw:
        return {}
    try:
        value = json.loads(str(raw))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return dict(value) if isinstance(value, Mapping) else {}


def _is_pending_paypal_authorization(row: Mapping[str, Any]) -> bool:
    payload = _raw_payload(row)
    return (
        str(payload.get("Tipo") or "").strip().casefold()
        == "blocco conto per autorizzazione aperta"
        and str(payload.get("Stato") or "").strip().casefold() == "in sospeso"
        and (
            str(payload.get("Nome") or "").strip().casefold() == "paypal"
            or "paypal" in str(row.get("import_filename") or "").casefold()
        )
    )


def _revolut_completed_raw_evidence(
    row: Mapping[str, Any], *, amount: Decimal
) -> dict[str, Any] | None:
    payload = _raw_payload(row)
    raw_id = row.get("raw_source_id")
    if raw_id is None:
        return None
    source_is_revolut = (
        str(row.get("import_source_type") or "").strip().casefold() == "xlsx_revolut"
        or str(payload.get("parser_layout") or "").strip().casefold() == "revolut_xlsx"
    )
    description = " ".join(
        str(payload.get(key) or "").strip()
        for key in ("descrizione_banca", "descrizione")
        if str(payload.get(key) or "").strip()
    )[:320]
    if (
        not source_is_revolut
        or str(payload.get("State") or "").strip().casefold() != "completato"
        or str(payload.get("Tipo") or "").strip().casefold() != "pagamento"
        or not description
    ):
        return None
    try:
        raw_amount = abs(_q2(payload.get("importo")))
    except Exception:
        return None
    if raw_amount != amount:
        return None
    payload_hash = hashlib.sha256(
        str(row.get("raw_payload_json") or "").encode("utf-8")
    ).hexdigest()
    return {
        "kind": "revolut_completed_payment_reference",
        "confidence": 95,
        "provenance_ref": f"runts:movement_raw:{int(raw_id)}",
        "description": description,
        "amount_eur": str(amount),
        "content_hash": payload_hash,
        "content_role": "completed_payment_statement_evidence",
        "fiscal_document": False,
    }


def _paypal_settled_raw_evidence(
    row: Mapping[str, Any], *, amount: Decimal
) -> dict[str, Any] | None:
    payload = _raw_payload(row)
    raw_id = row.get("raw_source_id")
    if raw_id is None:
        return None
    allowed_types = {
        "transazione generica con carta di debito paypal",
        "pagamento",
        "pagamento express checkout",
    }
    raw_type = str(payload.get("Tipo") or "").strip()
    merchant = " ".join(str(payload.get("Nome") or "").split())[:160]
    if (
        raw_type.casefold() not in allowed_types
        or str(payload.get("Stato") or "").strip().casefold() != "completata"
        or str(payload.get("Impatto sul saldo") or "").strip().casefold() != "addebito"
        or not merchant
    ):
        return None
    try:
        raw_amount = abs(_q2(payload.get("Lordo")))
    except Exception:
        return None
    if raw_amount != amount:
        return None
    transaction_id = str(payload.get("Codice transazione") or "").strip()
    payload_hash = hashlib.sha256(str(row.get("raw_payload_json") or "").encode("utf-8")).hexdigest()
    return {
        "kind": "paypal_raw_settled_transaction",
        "confidence": 97,
        "provenance_ref": f"runts:movement_raw:{int(raw_id)}",
        "merchant": merchant,
        "raw_type": raw_type,
        "amount_eur": str(amount),
        "transaction_reference_hash": (
            hashlib.sha256(transaction_id.encode("utf-8")).hexdigest()
            if transaction_id else None
        ),
        "content_hash": payload_hash,
        "content_role": "settled_payment_evidence",
        "fiscal_document": False,
    }


def _dedupe_replayed_import_rows(rows: list[Mapping[str, Any]]) -> tuple[list[dict[str, Any]], int]:
    """Represent an identical imported source once in the documentary review queue.

    This does not decide which ledger account owns the source.  It only suppresses
    replayed copies of the same source hash at the same source-row ordinal.
    """
    import_ids_by_hash: dict[str, set[int]] = {}
    for row in rows:
        digest = str(row["import_hash"] or "").strip()
        if digest:
            import_ids_by_hash.setdefault(digest, set()).add(int(row["import_id"]))
    replayed_hashes = {digest for digest, ids in import_ids_by_hash.items() if len(ids) > 1}
    passthrough: list[dict[str, Any]] = []
    grouped: dict[tuple[str, int], list[dict[str, Any]]] = {}
    for raw in rows:
        row = dict(raw)
        digest = str(row.get("import_hash") or "").strip()
        if digest not in replayed_hashes:
            row["source_duplicate_import_ids"] = []
            row["source_duplicate_account_ids"] = []
            row["source_duplicate_owner_unverified"] = False
            passthrough.append(row)
            continue
        key = (digest, int(row.get("import_row_ordinal") or 0))
        grouped.setdefault(key, []).append(row)
    representatives: list[dict[str, Any]] = []
    suppressed = 0
    for bucket in grouped.values():
        bucket.sort(key=lambda row: (str(row.get("import_timestamp") or ""), int(row["import_id"]), int(row["movement_id"])))
        representative = dict(bucket[0])
        import_ids = sorted({int(row["import_id"]) for row in bucket})
        account_ids = sorted({int(row["account_id"]) for row in bucket if row.get("account_id") is not None})
        representative["source_duplicate_import_ids"] = import_ids
        representative["source_duplicate_account_ids"] = account_ids
        representative["source_duplicate_owner_unverified"] = len(account_ids) > 1
        representatives.append(representative)
        suppressed += max(0, len(bucket) - 1)
    output = passthrough + representatives
    output.sort(key=lambda row: (-abs(_q2(row["importo_signed"])), str(row.get("data_movimento") or ""), int(row["movement_id"])))
    return output, suppressed


def _linked_project_documents(conn: sqlite3.Connection, movement_id: int) -> list[dict[str, Any]]:
    if not _table_exists(conn, "project_documents"):
        return []
    rows = conn.execute(
        """
        SELECT DISTINCT pd.*
        FROM project_documents pd
        LEFT JOIN project_document_links pdl ON pdl.document_id = pd.document_id
        WHERE pd.movement_id = ? OR pdl.movement_id = ?
        ORDER BY pd.document_id ASC
        """,
        (movement_id, movement_id),
    ).fetchall()
    return [dict(row) for row in rows]


def _candidate_project_documents(
    conn: sqlite3.Connection, *, movement_id: int, project_id: int | None,
    amount: Decimal, movement_date: str | None,
) -> list[dict[str, Any]]:
    if not _table_exists(conn, "project_documents"):
        return []
    params: list[Any] = [str(amount), movement_id]
    project_clause = ""
    if project_id is not None:
        project_clause = " AND (pd.project_id = ? OR pd.project_id IS NULL)"
        params.append(project_id)
    date_clause = ""
    if movement_date:
        date_clause = (
            " AND (pd.doc_date IS NULL OR "
            "ABS(julianday(pd.doc_date) - julianday(?)) <= 45)"
        )
        params.append(movement_date)
    rows = conn.execute(
        f"""
        SELECT pd.*
        FROM project_documents pd
        WHERE pd.total_amount IS NOT NULL
          AND ABS(ROUND(pd.total_amount, 2) - ROUND(CAST(? AS REAL), 2)) <= 0.01
          AND COALESCE(pd.movement_id, -1) <> ?
          {project_clause}
          {date_clause}
        ORDER BY CASE WHEN pd.status = 'ocr_done' THEN 0 ELSE 1 END,
                 pd.document_id ASC
        LIMIT 20
        """,
        tuple(params),
    ).fetchall()
    return [dict(row) for row in rows]


def _candidate_supporting_documents(
    conn: sqlite3.Connection, *, project_id: int | None, amount: Decimal,
    movement_date: str | None,
) -> list[dict[str, Any]]:
    if not _table_exists(conn, "supporting_documents"):
        return []
    params: list[Any] = [str(amount)]
    project_clause = ""
    if project_id is not None:
        project_clause = " AND (project_id = ? OR project_id IS NULL)"
        params.append(project_id)
    date_clause = ""
    if movement_date:
        date_clause = (
            " AND (data_documento IS NULL OR "
            "ABS(julianday(data_documento) - julianday(?)) <= 45)"
        )
        params.append(movement_date)
    rows = conn.execute(
        f"""
        SELECT * FROM supporting_documents
        WHERE totale_documento IS NOT NULL
          AND ABS(ROUND(totale_documento, 2) - ROUND(CAST(? AS REAL), 2)) <= 0.01
          {project_clause}
          {date_clause}
        ORDER BY document_id ASC
        LIMIT 20
        """,
        tuple(params),
    ).fetchall()
    return [dict(row) for row in rows]


def _candidate_supplier_invoices(
    conn: sqlite3.Connection, *, amount: Decimal, movement_date: str | None,
) -> list[dict[str, Any]]:
    if not (_table_exists(conn, "supplier_invoices") and _table_exists(conn, "suppliers")):
        return []
    params: list[Any] = [str(amount), str(amount)]
    date_clause = ""
    if movement_date:
        date_clause = " AND ABS(julianday(si.invoice_date) - julianday(?)) <= 90"
        params.append(movement_date)
    rows = conn.execute(
        f"""
        SELECT si.*, s.supplier_name
        FROM supplier_invoices si
        JOIN suppliers s ON s.supplier_id = si.supplier_id
        WHERE (
            ABS(ROUND(si.gross_amount, 2) - ROUND(CAST(? AS REAL), 2)) <= 0.01
            OR ABS(ROUND(si.net_amount, 2) - ROUND(CAST(? AS REAL), 2)) <= 0.01
        )
        {date_clause}
        ORDER BY si.supplier_invoice_id ASC
        LIMIT 20
        """,
        tuple(params),
    ).fetchall()
    return [dict(row) for row in rows]


_PAYPAL_TOPUP_DATE_RE = re.compile(r"\bDEL\s+(\d{2})/(\d{2})/(\d{2})\b", re.I)


def _paypal_topup_target_date(description: str | None, movement_date: str | None) -> str | None:
    target_date = movement_date
    match = _PAYPAL_TOPUP_DATE_RE.search(str(description or ""))
    if match:
        day, month, year2 = map(int, match.groups())
        try:
            target_date = date(2000 + year2, month, day).isoformat()
        except ValueError:
            return None
    return str(target_date) if target_date else None


def _candidate_paypal_balance_transfer(
    conn: sqlite3.Connection, *, amount: Decimal, movement_date: str | None,
    description: str | None, import_id: int,
) -> list[dict[str, Any]]:
    text = str(description or "")
    if "PAYPAL *ADD TO BAL" not in text.upper():
        return []
    target_date = _paypal_topup_target_date(text, movement_date)
    if not target_date:
        return []
    rows = conn.execute(
        """
        SELECT
            m.movement_id,
            m.import_id,
            m.account_id,
            m.data_movimento,
            m.importo_signed,
            m.descrizione_originale,
            i.filename,
            i.hash_file,
            i.account_id AS import_account_id
        FROM movements m
        JOIN imports i ON i.import_id = m.import_id
        WHERE m.importo_signed > 0
          AND ABS(ROUND(m.importo_signed, 2) - ROUND(CAST(? AS REAL), 2)) <= 0.01
          AND m.import_id <> ?
          AND ABS(julianday(m.data_movimento) - julianday(?)) <= 2
          AND (
                LOWER(COALESCE(i.filename,'')) LIKE '%paypal%'
                OR LOWER(COALESCE(m.descrizione_originale,'')) LIKE '%versamento generico con carta%'
          )
        ORDER BY ABS(julianday(m.data_movimento) - julianday(?)), m.movement_id
        LIMIT 20
        """,
        (str(amount), int(import_id), target_date, target_date),
    ).fetchall()
    return [dict(row) for row in rows]


def _promote_exact_paypal_transfer_groups(
    conn: sqlite3.Connection, cases: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in cases:
        if row.get("ready_for_human_confirmation"):
            continue
        description = str(row.get("description") or "")
        if "PAYPAL *ADD TO BAL" not in description.upper():
            continue
        target_date = _paypal_topup_target_date(description, str(row.get("movement_date") or ""))
        if not target_date:
            continue
        key = (str(_q2(row.get("amount_eur"))), target_date)
        groups.setdefault(key, []).append(row)
    for (amount, target_date), group in groups.items():
        candidates = [dict(row) for row in conn.execute(
            """
            SELECT m.movement_id, m.import_id, m.account_id, m.data_movimento,
                   m.importo_signed, m.descrizione_originale, i.filename, i.hash_file
            FROM movements m
            JOIN imports i ON i.import_id = m.import_id
            WHERE m.importo_signed > 0
              AND ABS(ROUND(m.importo_signed, 2) - ROUND(CAST(? AS REAL), 2)) <= 0.01
              AND m.data_movimento = ?
              AND (
                    LOWER(COALESCE(i.filename,'')) LIKE '%paypal%'
                    OR LOWER(COALESCE(m.descrizione_originale,'')) LIKE '%versamento generico con carta%'
              )
            ORDER BY m.import_id, m.movement_id
            """,
            (amount, target_date),
        ).fetchall()]
        if not candidates or len(candidates) != len(group):
            continue
        candidate_ids = sorted(int(row["movement_id"]) for row in candidates)
        fingerprint = hashlib.sha256(
            json.dumps(
                {
                    "amount_eur": amount,
                    "target_date": target_date,
                    "debit_movement_ids": sorted(int(row["movement_id"]) for row in group),
                    "credit_movement_ids": candidate_ids,
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        for row in group:
            movement_id = str(row["movement_id"])
            provenance_ref = f"runts:paypal_balance_transfer_group:{fingerprint}:debit:{movement_id}"
            evidence = {
                "kind": "paypal_balance_transfer_group_match",
                "confidence": 96,
                "provenance_ref": provenance_ref,
                "candidate_credit_movement_ids": candidate_ids,
                "candidate_count": len(candidate_ids),
                "debit_group_count": len(group),
                "target_date": target_date,
                "amount_eur": amount,
                "economic_role": "internal_transfer_candidate",
                "pairing_identity_unresolved": len(group) > 1,
                "fiscal_document": False,
            }
            external = list(row.get("external_evidence") or ())
            external.append(evidence)
            row["external_evidence"] = external
            refs = list(row.get("evidence_refs") or ())
            refs.append(provenance_ref)
            row["evidence_refs"] = list(dict.fromkeys(str(ref) for ref in refs if str(ref)))
            row["ready_for_human_confirmation"] = True
            row["suggested_human_decision"] = "confirm_internal_transfer"
            row["reconstruction_evidence_score"] = max(
                int(row.get("reconstruction_evidence_score") or 0), 96
            )
            row["paypal_balance_transfer_group_match"] = True
    return cases


def _candidate_amazon_orders(
    conn: sqlite3.Connection, *, amount: Decimal, movement_date: str | None, description: str | None,
) -> list[dict[str, Any]]:
    if not _table_exists(conn, "amazon_orders") or not movement_date:
        return []
    haystack = str(description or "").casefold()
    if "amazon" not in haystack and "amzn" not in haystack:
        return []
    rows = conn.execute(
        """
        SELECT order_id, order_date, total_amount, currency, payment_method
        FROM amazon_orders
        WHERE ABS(ROUND(total_amount, 2) - ROUND(CAST(? AS REAL), 2)) <= 0.01
          AND ABS(julianday(order_date) - julianday(?)) <= 7
        ORDER BY ABS(julianday(order_date) - julianday(?)), order_id
        LIMIT 20
        """,
        (str(amount), movement_date, movement_date),
    ).fetchall()
    return [dict(row) for row in rows]


def _candidate_refs(
    *, project_documents: list[Mapping[str, Any]],
    supporting_documents: list[Mapping[str, Any]],
    supplier_invoices: list[Mapping[str, Any]],
) -> tuple[list[str], int]:
    refs: list[str] = []
    score = 0
    for row in project_documents:
        refs.append(f"runts:project_document_candidate:{row['document_id']}")
        score += 60 if row.get("status") == "ocr_done" else 45
    for row in supporting_documents:
        refs.append(f"runts:supporting_document_candidate:{row['document_id']}")
        score += 55
    for row in supplier_invoices:
        refs.append(f"runts:supplier_invoice_candidate:{row['supplier_invoice_id']}")
        score += 40 if str(row.get("document_path") or "").strip() else 25
    return refs, min(score, 100)


def build_runts_document_case(
    conn: sqlite3.Connection, row: Mapping[str, Any], *,
    human_decision: str | None = None,
) -> dict[str, Any]:
    movement_id = int(row["movement_id"])
    amount = -_q2(row["importo_signed"])
    project_id = row.get("project_id_manuale") or row.get("project_id")
    linked = _linked_project_documents(conn, movement_id)
    candidates_pd = _candidate_project_documents(
        conn,
        movement_id=movement_id,
        project_id=int(project_id) if project_id is not None else None,
        amount=amount,
        movement_date=row.get("data_movimento"),
    )
    candidates_sd = _candidate_supporting_documents(
        conn,
        project_id=int(project_id) if project_id is not None else None,
        amount=amount,
        movement_date=row.get("data_movimento"),
    )
    candidates_si = _candidate_supplier_invoices(
        conn,
        amount=amount,
        movement_date=row.get("data_movimento"),
    )
    candidates_paypal_transfer = _candidate_paypal_balance_transfer(
        conn,
        amount=amount,
        movement_date=row.get("data_movimento"),
        description=row.get("descrizione_originale"),
        import_id=int(row["import_id"]),
    )
    candidates_amazon = _candidate_amazon_orders(
        conn,
        amount=amount,
        movement_date=row.get("data_movimento"),
        description=" ".join(str(value or "") for value in (row.get("descrizione_originale"), row.get("contropartita"))),
    )
    raw_paypal_evidence = _paypal_settled_raw_evidence(row, amount=amount)
    raw_revolut_evidence = _revolut_completed_raw_evidence(row, amount=amount)

    original_refs = [f"runts:project_document:{doc['document_id']}" for doc in linked]
    candidate_refs, candidate_score = _candidate_refs(
        project_documents=candidates_pd,
        supporting_documents=candidates_sd,
        supplier_invoices=candidates_si,
    )
    duplicate_import_ids = [int(value) for value in row.get("source_duplicate_import_ids") or ()]
    represented_import_ids = duplicate_import_ids or [int(row["import_id"])]
    payment_refs = [f"runts:movement:{movement_id}"] + [
        f"runts:import:{import_id}" for import_id in represented_import_ids
    ]
    context_refs: list[str] = list(candidate_refs)
    if raw_paypal_evidence is not None:
        context_refs.append(str(raw_paypal_evidence["provenance_ref"]))
    if raw_revolut_evidence is not None:
        context_refs.append(str(raw_revolut_evidence["provenance_ref"]))
    paypal_transfer_unique = candidates_paypal_transfer[0] if len(candidates_paypal_transfer) == 1 else None
    if paypal_transfer_unique is not None:
        context_refs.extend([
            f"runts:movement:{int(paypal_transfer_unique['movement_id'])}",
            f"runts:import:{int(paypal_transfer_unique['import_id'])}",
        ])
    amazon_unique = candidates_amazon[0] if len(candidates_amazon) == 1 else None
    if amazon_unique is not None:
        context_refs.append(f"runts:amazon_order:{amazon_unique['order_id']}")
    if project_id is not None:
        context_refs.append(f"runts:project:{int(project_id)}")
    category_id = row.get("runts_category_id_override") or row.get("category_id_manuale") or row.get("category_id")
    if category_id is not None:
        context_refs.append(f"runts:category:{int(category_id)}")
    if row.get("review_decision"):
        context_refs.append(f"runts:movement_review:{movement_id}")
    if str(row.get("contropartita") or "").strip():
        context_refs.append(f"runts:movement:{movement_id}:counterparty")
    if str(row.get("notes") or "").strip():
        context_refs.append(f"runts:movement:{movement_id}:notes")

    review = review_missing_document_case({
        "movement_id": str(movement_id),
        "amount": str(amount),
        "counterparty": row.get("contropartita"),
        "proposed_category": row.get("category_name"),
        "original_document_refs": original_refs,
        "payment_refs": payment_refs,
        "context_refs": context_refs,
        "human_decision": human_decision,
    })
    base_score = 20
    if project_id is not None:
        base_score += 10
    if category_id is not None:
        base_score += 10
    if str(row.get("contropartita") or "").strip():
        base_score += 10
    if row.get("review_decision"):
        base_score += 10
    if str(row.get("notes") or "").strip():
        base_score += 5
    if paypal_transfer_unique is not None:
        base_score += 45
    if amazon_unique is not None:
        base_score += 35
    evidence_score = 100 if original_refs else min(98 if paypal_transfer_unique is not None else 95, base_score + candidate_score)
    if raw_paypal_evidence is not None:
        evidence_score = max(evidence_score, int(raw_paypal_evidence["confidence"]))
    if raw_revolut_evidence is not None:
        evidence_score = max(evidence_score, int(raw_revolut_evidence["confidence"]))
    external_evidence = []
    if raw_paypal_evidence is not None:
        external_evidence.append(dict(raw_paypal_evidence))
    if raw_revolut_evidence is not None:
        external_evidence.append(dict(raw_revolut_evidence))
    if paypal_transfer_unique is not None:
        external_evidence.append({
            "kind": "paypal_balance_transfer_pair",
            "confidence": 98,
            "provenance_ref": f"runts:movement:{int(paypal_transfer_unique['movement_id'])}",
            "paired_movement_id": int(paypal_transfer_unique["movement_id"]),
            "paired_import_id": int(paypal_transfer_unique["import_id"]),
            "date": paypal_transfer_unique.get("data_movimento"),
            "amount_eur": str(_q2(paypal_transfer_unique.get("importo_signed"))),
            "economic_role": "internal_transfer_candidate",
            "account_assignment_unverified": int(paypal_transfer_unique.get("account_id") or -1) == int(row.get("account_id") or -2),
            "fiscal_document": False,
        })
    if amazon_unique is not None:
        external_evidence.append({
            "kind": "amazon_order",
            "confidence": 90,
            "provenance_ref": f"runts:amazon_order:{amazon_unique['order_id']}",
            "order_id": amazon_unique["order_id"],
            "date": amazon_unique.get("order_date"),
            "amount_eur": str(_q2(amazon_unique.get("total_amount"))),
            "payment_method": amazon_unique.get("payment_method"),
            "fiscal_document": False,
        })
    review.update({
        "movement_date": row.get("data_movimento"),
        "description": row.get("descrizione_originale"),
        "account_id": row.get("account_id"),
        "account_name": row.get("account_name"),
        "project_id": project_id,
        "category_id": category_id,
        "category_name": row.get("category_name"),
        "validation_status": row.get("stato_validazione"),
        "source_duplicate_import_ids": duplicate_import_ids,
        "source_duplicate_account_ids": list(row.get("source_duplicate_account_ids") or ()),
        "source_duplicate_owner_unverified": bool(row.get("source_duplicate_owner_unverified")),
        "reconstruction_evidence_score": evidence_score,
        "candidate_document_refs": candidate_refs,
        "candidate_document_count": len(candidate_refs),
        "paypal_balance_transfer_candidate_count": len(candidates_paypal_transfer),
        "amazon_order_candidate_count": len(candidates_amazon),
        "paypal_raw_settled_match": raw_paypal_evidence is not None,
        "revolut_completed_payment_match": raw_revolut_evidence is not None,
        "external_evidence": external_evidence,
        "suggested_human_decision": "confirm_internal_transfer" if paypal_transfer_unique is not None else "approve_reconstruction",
        "ready_for_human_confirmation": bool(
            paypal_transfer_unique or amazon_unique or raw_paypal_evidence or raw_revolut_evidence
        ),
        "live_source": "runts_suite_sqlite_readonly",
    })
    return review


def latest_expense_year(db_path: str | Path) -> int | None:
    with open_runts_readonly(db_path) as conn:
        row = conn.execute(
            """
            SELECT MAX(CAST(substr(data_movimento, 1, 4) AS INTEGER))
            FROM movements
            WHERE importo_signed < 0
            """
        ).fetchone()
    if not row or row[0] is None:
        return None
    year = int(row[0])
    return year if 2000 <= year <= 2100 else None


def audit_runts_missing_documents(
    db_path: str | Path, *, year: int,
    human_decisions: Mapping[str, str] | None = None,
    evidence_snapshot: Mapping[str, Any] | None = None,
    limit: int | None = None,
) -> dict[str, Any]:
    decisions = dict(human_decisions or {})
    with open_runts_readonly(db_path) as conn:
        data_version = int(conn.execute("PRAGMA data_version").fetchone()[0])
        raw_rows = _expense_rows(conn, year)
        source_rows = _attach_raw_source_rows(conn, raw_rows)
        rows, source_duplicate_suppressed_count = _dedupe_replayed_import_rows(source_rows)
        paypal_pending_holds = [row for row in rows if _is_pending_paypal_authorization(row)]
        economic_rows = [row for row in rows if not _is_pending_paypal_authorization(row)]
        cases = [
            build_runts_document_case(
                conn,
                row,
                human_decision=decisions.get(str(row["movement_id"])),
            )
            for row in economic_rows
        ]
        cases = _promote_exact_paypal_transfer_groups(conn, cases)
    from .accounting_external_evidence import enrich_rows_with_bank_native, enforce_unique_external_evidence
    cases = enrich_rows_with_bank_native(cases)
    if evidence_snapshot is not None:
        from .accounting_external_evidence import enrich_rows_with_paypal
        cases = enrich_rows_with_paypal(cases, evidence_snapshot)
    cases = enforce_unique_external_evidence(cases)
    cases.sort(
        key=lambda item: (
            item["original_document_status"] == "PRESENT",
            not bool(item.get("ready_for_human_confirmation")),
            -int(item.get("reconstruction_evidence_score") or 0),
            -_q2(item["amount_eur"]),
            int(item["movement_id"]),
        )
    )
    review_required_count = sum(1 for row in cases if row.get("human_review_required"))
    approved_reconstruction_count = sum(
        1 for row in cases if row.get("accounting_status") == "HUMAN_APPROVED_RECONSTRUCTION"
    )
    raw_total_amount = sum((-_q2(row["importo_signed"]) for row in raw_rows), Decimal("0"))
    paypal_pending_hold_total = sum(
        (-_q2(row["importo_signed"]) for row in paypal_pending_holds), Decimal("0")
    )
    total_amount = sum((_q2(row["amount_eur"]) for row in cases), Decimal("0"))
    missing_amount = sum(
        (_q2(row["amount_eur"]) for row in cases if row["original_document_status"] == "MISSING"),
        Decimal("0"),
    )
    with_candidates = [row for row in cases if row.get("candidate_document_count")]
    linked = [row for row in cases if row["original_document_status"] == "PRESENT"]
    ready = [row for row in cases if row.get("ready_for_human_confirmation") and row["original_document_status"] == "MISSING"]
    amazon_matches = [row for row in cases if any(ev.get("kind") == "amazon_order" for ev in row.get("external_evidence") or ())]
    paypal_matches = [row for row in cases if any(ev.get("kind") == "paypal_email_receipt" for ev in row.get("external_evidence") or ())]
    paypal_transfer_pairs = [row for row in cases if any(ev.get("kind") == "paypal_balance_transfer_pair" for ev in row.get("external_evidence") or ())]
    paypal_transfer_groups = [row for row in cases if any(ev.get("kind") == "paypal_balance_transfer_group_match" for ev in row.get("external_evidence") or ())]
    paypal_raw_settled_matches = [row for row in cases if any(ev.get("kind") == "paypal_raw_settled_transaction" for ev in row.get("external_evidence") or ())]
    revolut_completed_matches = [row for row in cases if any(ev.get("kind") == "revolut_completed_payment_reference" for ev in row.get("external_evidence") or ())]
    bank_native_matches = [
        row for row in cases
        if any(str(ev.get("kind") or "").startswith("bank_") for ev in row.get("external_evidence") or ())
    ]
    from .accounting_review import build_human_confirmation_batch
    confirmation_batch = build_human_confirmation_batch(cases, year=year)
    displayed = cases if limit is None else cases[: max(0, int(limit))]
    return {
        "source": {
            "kind": "runts_suite_sqlite",
            "mode": "read_only",
            "db_path": str(Path(db_path).expanduser().resolve()),
            "data_version": data_version,
            "year": year,
            "external_evidence_snapshot_sha256": (
                str(evidence_snapshot.get("_snapshot_sha256"))
                if isinstance(evidence_snapshot, Mapping) and evidence_snapshot.get("_snapshot_sha256")
                else None
            ),
        },
        "summary": {
            "raw_expense_movement_count": len(raw_rows),
            "raw_expense_total_eur": str(raw_total_amount),
            "source_duplicate_suppressed_count": source_duplicate_suppressed_count,
            "paypal_pending_authorization_suppressed_count": len(paypal_pending_holds),
            "paypal_pending_authorization_suppressed_total_eur": str(paypal_pending_hold_total),
            "expense_movement_count": len(cases),
            "expense_total_eur": str(total_amount),
            "linked_original_count": len(linked),
            "missing_original_count": len(cases) - len(linked),
            "missing_original_total_eur": str(missing_amount),
            "candidate_document_match_count": len(with_candidates),
            "amazon_order_match_count": len(amazon_matches),
            "paypal_email_receipt_match_count": len(paypal_matches),
            "paypal_balance_transfer_pair_count": len(paypal_transfer_pairs),
            "paypal_balance_transfer_group_match_count": len(paypal_transfer_groups),
            "paypal_raw_settled_match_count": len(paypal_raw_settled_matches),
            "revolut_completed_payment_match_count": len(revolut_completed_matches),
            "bank_native_reference_match_count": len(bank_native_matches),
            "ready_for_human_confirmation_count": len(ready),
            "unresolved_evidence_count": max(0, (len(cases) - len(linked)) - len(ready)),
            "human_review_required_count": review_required_count,
            "human_approved_reconstruction_count": approved_reconstruction_count,
            "invented_documents": 0,
        },
        "human_confirmation_batch": confirmation_batch,
        "rows": displayed,
        "total_rows_before_limit": len(cases),
        "invariants": {
            "invented_documents": 0,
            "human_approval_required_for_missing_original": True,
            "bookkeeping_reconstruction_does_not_imply_tax_or_grant_eligibility": True,
        },
    }


__all__ = [
    "audit_runts_missing_documents",
    "build_runts_document_case",
    "latest_expense_year",
    "open_runts_readonly",
]
