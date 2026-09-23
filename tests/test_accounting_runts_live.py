from __future__ import annotations

import hashlib
import sqlite3

from ralfloop_agent.unified_assistant.accounting_runts_live import audit_runts_missing_documents


def build_db(path):
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE accounts (
            account_id INTEGER PRIMARY KEY,
            nome_account TEXT,
            is_operational_for_association INTEGER,
            requires_reimbursement INTEGER
        );
        CREATE TABLE categories (
            category_id INTEGER PRIMARY KEY,
            nome_categoria TEXT,
            tipo TEXT
        );
        CREATE TABLE movement_reviews (
            movement_id INTEGER PRIMARY KEY,
            decisione TEXT,
            runts_excluded INTEGER DEFAULT 0,
            runts_detached INTEGER DEFAULT 0,
            runts_category_id_override INTEGER,
            category_id_manuale INTEGER,
            internal_category_id_manuale INTEGER,
            project_id_manuale INTEGER,
            note_revisione TEXT
        );
        CREATE TABLE movements (
            movement_id INTEGER PRIMARY KEY,
            import_id INTEGER NOT NULL,
            account_id INTEGER,
            data_movimento TEXT NOT NULL,
            descrizione_originale TEXT NOT NULL,
            importo_signed NUMERIC NOT NULL,
            contropartita TEXT,
            category_id INTEGER,
            project_id INTEGER,
            internal_category_id INTEGER,
            is_transfer INTEGER DEFAULT 0,
            is_duplicate INTEGER DEFAULT 0,
            movimento_dummy INTEGER DEFAULT 0,
            movement_kind TEXT,
            notes TEXT,
            stato_validazione TEXT
        );
        CREATE TABLE project_documents (
            document_id INTEGER PRIMARY KEY,
            project_id INTEGER,
            filename TEXT,
            status TEXT,
            doc_date TEXT,
            total_amount REAL,
            movement_id INTEGER
        );
        CREATE TABLE project_document_links (
            link_id INTEGER PRIMARY KEY,
            document_id INTEGER,
            movement_id INTEGER
        );
        CREATE TABLE supporting_documents (
            document_id INTEGER PRIMARY KEY,
            project_id INTEGER,
            data_documento TEXT,
            totale_documento REAL
        );
        CREATE TABLE suppliers (
            supplier_id INTEGER PRIMARY KEY,
            supplier_name TEXT
        );
        CREATE TABLE supplier_invoices (
            supplier_invoice_id INTEGER PRIMARY KEY,
            supplier_id INTEGER,
            invoice_date TEXT,
            gross_amount REAL,
            net_amount REAL,
            document_path TEXT
        );
        INSERT INTO accounts VALUES (1,'Banca',1,0),(9,'Socio',0,1);
        INSERT INTO categories VALUES
            (5,'Materiale didattico','uscita'),
            (6,'Trasferimenti','trasferimento');
        INSERT INTO movements (
            movement_id,import_id,account_id,data_movimento,descrizione_originale,
            importo_signed,contropartita,category_id,project_id,internal_category_id,
            is_transfer,is_duplicate,movement_kind,notes,stato_validazione
        ) VALUES
            (1,101,1,'2025-03-10','Cartoleria',-42.50,'Cartoleria Fixture',5,1,NULL,0,0,NULL,NULL,'classificato_auto'),
            (2,102,1,'2025-04-12','Affitto sala',-100.00,'Sala Fixture',5,1,NULL,0,0,NULL,NULL,'manuale'),
            (3,103,1,'2025-05-01','Giroconto',-20.00,'',6,NULL,NULL,1,0,NULL,NULL,'manuale'),
            (4,104,1,'2025-05-02','Duplicato',-30.00,'',5,NULL,NULL,0,1,NULL,NULL,'manuale'),
            (5,105,1,'2025-05-03','Entrata',50.00,'',5,NULL,NULL,0,0,NULL,NULL,'manuale'),
            (6,106,1,'2025-05-04','Categoria trasferimento',-15.00,'',6,NULL,NULL,0,0,NULL,NULL,'manuale'),
            (7,107,9,'2025-06-01','Anticipo socio',-85.00,'ARCI',5,NULL,NULL,0,0,'member_advance','Spesa per ente','manuale');
        INSERT INTO project_documents VALUES
            (10,1,'receipt-linked.pdf','ocr_done','2025-04-12',100.00,2),
            (11,1,'receipt-candidate.pdf','ocr_done','2025-03-10',42.50,NULL);
        """
    )
    conn.commit()
    conn.close()


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_live_audit_reads_realistic_runts_schema_without_writes(tmp_path):
    db = tmp_path / "runts_suite.db"
    build_db(db)
    before = sha256(db)
    result = audit_runts_missing_documents(db, year=2025)
    after = sha256(db)

    assert before == after
    assert result["source"]["mode"] == "read_only"
    assert result["summary"]["expense_movement_count"] == 3
    assert result["summary"]["expense_total_eur"] == "227.50"
    assert result["summary"]["linked_original_count"] == 1
    assert result["summary"]["missing_original_count"] == 2
    assert result["summary"]["human_review_required_count"] == 2
    assert result["summary"]["invented_documents"] == 0


def test_exact_unlinked_document_is_candidate_not_invented_original(tmp_path):
    db = tmp_path / "runts_suite.db"
    build_db(db)
    result = audit_runts_missing_documents(db, year=2025)
    row = next(item for item in result["rows"] if item["movement_id"] == "1")
    assert row["original_document_status"] == "MISSING"
    assert row["candidate_document_count"] == 1
    assert row["candidate_document_refs"] == ["runts:project_document_candidate:11"]
    assert row["evidence_status"] == "CORROBORATED_MISSING_ORIGINAL"
    assert row["reconstruction_evidence_score"] >= 80


def test_linked_document_is_documented_and_human_decision_can_approve_reconstruction(tmp_path):
    db = tmp_path / "runts_suite.db"
    build_db(db)
    result = audit_runts_missing_documents(
        db,
        year=2025,
        human_decisions={"1": "approve_reconstruction"},
    )
    linked = next(item for item in result["rows"] if item["movement_id"] == "2")
    reconstructed = next(item for item in result["rows"] if item["movement_id"] == "1")
    assert linked["original_document_status"] == "PRESENT"
    assert linked["accounting_status"] == "DOCUMENTED"
    assert reconstructed["original_document_status"] == "MISSING"
    assert reconstructed["accounting_status"] == "HUMAN_APPROVED_RECONSTRUCTION"
    assert reconstructed["external_eligibility"] == "REQUIRES_SEPARATE_RULE_CHECK"
    assert result["summary"]["human_approved_reconstruction_count"] == 1


def test_limit_only_limits_rendered_rows_not_summary(tmp_path):
    db = tmp_path / "runts_suite.db"
    build_db(db)
    result = audit_runts_missing_documents(db, year=2025, limit=1)
    assert len(result["rows"]) == 1
    assert result["total_rows_before_limit"] == 3
    assert result["summary"]["expense_movement_count"] == 3

from ralfloop_agent.unified_assistant.accounting import accounting_read_adapter
from ralfloop_agent.unified_assistant.contracts import PlanAssignment, PolicyClass


def _assignment(objective):
    return PlanAssignment(
        task_id="task.live-runts-fixture",
        domain="accounting",
        skill="accounting.read",
        objective=objective,
        input_refs=("user.goal",),
        output_ref="artifact.accounting",
        policy=PolicyClass.READ,
    )


def test_commercialista_adapter_reads_live_runts_queue(tmp_path):
    db = tmp_path / "runts_suite.db"
    build_db(db)
    artifact = accounting_read_adapter(
        _assignment("Controlla le ricevute perse del 2025"),
        {
            "accounting.runts_db_path": str(db),
            "accounting.review_limit": 2,
        },
    )
    assert artifact.status == "clarification_required"
    assert artifact.payload["human_review_required"] is True
    assert artifact.payload["runts_live_audit"]["summary"]["expense_movement_count"] == 3
    assert artifact.payload["document_review_queue"]["case_count"] == 3
    assert len(artifact.payload["document_review_queue"]["rows"]) == 2
    assert "mostrati 2 prioritari" in artifact.payload["message"]
    assert artifact.payload["writes"] == 0


def test_commercialista_reconciliation_can_use_latest_live_year(tmp_path):
    db = tmp_path / "runts_suite.db"
    build_db(db)
    artifact = accounting_read_adapter(
        _assignment("Fammi quadrare il gestionale"),
        {"accounting.runts_db_path": str(db)},
    )
    assert artifact.status == "completed"
    assert artifact.payload["runts_live_audit"]["source"]["year"] == 2025
    assert "3 uscite" in artifact.payload["message"]
    assert "sola lettura" in artifact.payload["message"]


def test_amazon_order_is_strong_context_evidence_but_not_fiscal_document(tmp_path):
    db = tmp_path / "runts_suite.db"
    build_db(db)
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE amazon_orders(order_id TEXT PRIMARY KEY, order_date TEXT, total_amount REAL, currency TEXT, payment_method TEXT)")
    conn.execute("UPDATE movements SET descrizione_originale='Amazon.it', contropartita='Amazon.it' WHERE movement_id=1")
    conn.execute("INSERT INTO amazon_orders VALUES(?,?,?,?,?)", ("ORDER-1","2025-03-09",42.50,"EUR","MasterCard - 2055"))
    conn.commit(); conn.close()
    result = audit_runts_missing_documents(db, year=2025)
    row = next(item for item in result["rows"] if item["movement_id"] == "1")
    assert row["amazon_order_candidate_count"] == 1
    assert row["ready_for_human_confirmation"] is True
    assert row["original_document_status"] == "MISSING"
    assert row["human_review_required"] is True
    assert row["external_evidence"][0]["kind"] == "amazon_order"
    assert row["external_evidence"][0]["fiscal_document"] is False
    assert result["summary"]["amazon_order_match_count"] == 1
    assert result["summary"]["ready_for_human_confirmation_count"] == 1


def test_commercialista_adapter_consumes_external_evidence_snapshot(tmp_path):
    import json
    db = tmp_path / "runts_suite.db"
    build_db(db)
    conn = sqlite3.connect(db)
    conn.execute("UPDATE movements SET descrizione_originale='Cartoleria Fixture', contropartita='Cartoleria Fixture' WHERE movement_id=1")
    conn.commit()
    conn.close()
    snapshot = tmp_path / "evidence.json"
    snapshot.write_text(json.dumps({
        "schema_version": 1,
        "receipts": [{
            "kind": "paypal_email_receipt",
            "message_id": "abc",
            "date": "2025-03-09",
            "amount_eur": "42.50",
            "merchant": "Cartoleria Fixture",
            "transaction_id": "TX1",
            "card_suffix": "2055",
            "provenance_ref": "gmail:message:abc",
            "content_hash": "a" * 64,
            "fiscal_document": False,
        }],
    }))
    artifact = accounting_read_adapter(
        _assignment("Controlla le ricevute perse del 2025"),
        {
            "accounting.runts_db_path": str(db),
            "accounting.evidence_snapshot_path": str(snapshot),
            "accounting.review_limit": 10,
        },
    )
    audit = artifact.payload["runts_live_audit"]
    assert audit["summary"]["paypal_email_receipt_match_count"] == 1
    assert audit["summary"]["ready_for_human_confirmation_count"] == 1
    assert audit["human_confirmation_batch"]["item_count"] == 1
    assert audit["human_confirmation_batch"]["executable"] is False
    assert "gmail:message:abc" in artifact.evidence_refs
    assert "pronti per conferma umana" in artifact.payload["message"]
