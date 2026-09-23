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
        CREATE TABLE imports (
            import_id INTEGER PRIMARY KEY,
            filename TEXT NOT NULL,
            source_type TEXT NOT NULL,
            import_timestamp TEXT NOT NULL,
            hash_file TEXT,
            parser_usato TEXT,
            account_id INTEGER
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
        INSERT INTO imports(import_id,filename,source_type,import_timestamp,hash_file,parser_usato,account_id) VALUES
            (101,'a.csv','bank','2025-01-01T00:00:01','h101','fixture',1),
            (102,'b.csv','bank','2025-01-01T00:00:02','h102','fixture',1),
            (103,'c.csv','bank','2025-01-01T00:00:03','h103','fixture',1),
            (104,'d.csv','bank','2025-01-01T00:00:04','h104','fixture',1),
            (105,'e.csv','bank','2025-01-01T00:00:05','h105','fixture',1),
            (106,'f.csv','bank','2025-01-01T00:00:06','h106','fixture',1),
            (107,'g.csv','bank','2025-01-01T00:00:07','h107','fixture',9);
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


def test_paypal_add_to_balance_pair_is_transfer_candidate_until_human_confirms(tmp_path):
    db = tmp_path / "runts_suite.db"
    build_db(db)
    conn = sqlite3.connect(db)
    conn.execute("INSERT INTO accounts VALUES (2,'PayPal',1,0)")
    conn.executemany(
        "INSERT INTO imports(import_id,filename,source_type,import_timestamp,hash_file,parser_usato,account_id) VALUES(?,?,?,?,?,?,?)",
        [
            (108,'bank.xlsx','xlsx','2026-01-01T00:00:01','bankhash','fixture',1),
            (109,'paypal.CSV','csv','2026-01-01T00:00:02','paypalhash','fixture',2),
        ],
    )
    conn.executemany(
        "INSERT INTO movements(movement_id,import_id,account_id,data_movimento,descrizione_originale,importo_signed,contropartita,category_id,project_id,internal_category_id,is_transfer,is_duplicate,movement_kind,notes,stato_validazione) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [
            (8,108,1,'2025-07-13','Pagamenti paesi UE DEL 10/07/25 IN ITALIA C/O PAYPAL *ADD TO BAL CARTA N. 1006',-50,'',5,None,None,0,0,None,None,'da_rivedere'),
            (9,109,2,'2025-07-10','Versamento generico con carta',50,'fabio@example.invalid',None,None,None,0,0,None,None,'importato_pdf'),
        ],
    )
    conn.commit(); conn.close()
    result = audit_runts_missing_documents(db, year=2025)
    row = next(item for item in result["rows"] if item["movement_id"] == "8")
    assert row["paypal_balance_transfer_candidate_count"] == 1
    assert row["ready_for_human_confirmation"] is True
    assert row["suggested_human_decision"] == "confirm_internal_transfer"
    assert row["external_evidence"][0]["kind"] == "paypal_balance_transfer_pair"
    assert row["external_evidence"][0]["fiscal_document"] is False
    assert result["summary"]["paypal_balance_transfer_pair_count"] == 1
    confirmed = audit_runts_missing_documents(db, year=2025, human_decisions={"8":"confirm_internal_transfer"})
    confirmed_row = next(item for item in confirmed["rows"] if item["movement_id"] == "8")
    assert confirmed_row["accounting_status"] == "HUMAN_CONFIRMED_INTERNAL_TRANSFER"
    assert confirmed_row["external_eligibility"] == "EXCLUDED_INTERNAL_TRANSFER"
    assert confirmed_row["human_review_required"] is False


def test_replayed_identical_import_is_represented_once_without_assigning_owner(tmp_path):
    db = tmp_path / "runts_suite.db"
    build_db(db)
    conn = sqlite3.connect(db)
    conn.execute("INSERT INTO accounts VALUES (2,'Replay account',1,0)")
    conn.executemany(
        "INSERT INTO imports(import_id,filename,source_type,import_timestamp,hash_file,parser_usato,account_id) VALUES(?,?,?,?,?,?,?)",
        [
            (108,'same-a.pdf','pdf','2026-01-01T00:00:01','samehash','fixture',1),
            (109,'same-b.pdf','pdf','2026-01-01T00:00:02','samehash','fixture',2),
        ],
    )
    conn.executemany(
        "INSERT INTO movements(movement_id,import_id,account_id,data_movimento,descrizione_originale,importo_signed,contropartita,category_id,project_id,internal_category_id,is_transfer,is_duplicate,movement_kind,notes,stato_validazione) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [
            (8,108,1,'2025-08-01','Prelievo Con Bonifico',-70,'',5,None,None,0,0,None,None,'da_rivedere'),
            (9,109,2,'2025-08-01','Prelievo Con Bonifico',-70,'',5,None,None,0,0,None,None,'da_rivedere'),
        ],
    )
    conn.commit(); conn.close()
    result = audit_runts_missing_documents(db, year=2025)
    assert result["summary"]["raw_expense_movement_count"] == 5
    assert result["summary"]["source_duplicate_suppressed_count"] == 1
    assert result["summary"]["expense_movement_count"] == 4
    replay = next(row for row in result["rows"] if row["source_duplicate_import_ids"])
    assert replay["source_duplicate_import_ids"] == [108,109]
    assert replay["source_duplicate_account_ids"] == [1,2]
    assert replay["source_duplicate_owner_unverified"] is True
    assert replay["ready_for_human_confirmation"] is False


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

def test_pending_paypal_authorization_is_suppressed_and_settled_raw_payment_is_recovered(tmp_path):
    import json
    db = tmp_path / "runts_suite.db"
    build_db(db)
    conn = sqlite3.connect(db)
    conn.execute("""
        CREATE TABLE movements_raw (
            raw_id INTEGER PRIMARY KEY,
            import_id INTEGER NOT NULL,
            row_number INTEGER NOT NULL,
            payload_originale_json TEXT NOT NULL
        )
    """)
    conn.execute(
        "INSERT INTO imports(import_id,filename,source_type,import_timestamp,hash_file,parser_usato,account_id) VALUES(?,?,?,?,?,?,?)",
        (108, "paypal.CSV", "csv_generic", "2026-01-01T00:00:01", "paypal-hash", "csv_upload_v1", 1),
    )
    conn.executemany(
        """INSERT INTO movements(
            movement_id,import_id,account_id,data_movimento,descrizione_originale,
            importo_signed,contropartita,category_id,project_id,internal_category_id,
            is_transfer,is_duplicate,movement_kind,notes,stato_validazione
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        [
            (8,108,1,"2025-01-22","PayPal",-4.72,"PayPal",5,None,None,0,0,None,None,"da_rivedere"),
            (9,108,1,"2025-01-23","PayPal",-54.99,"PayPal",5,None,None,0,0,None,None,"da_rivedere"),
            (10,108,1,"2025-01-23","PayPal",4.72,"PayPal",5,None,None,0,0,None,None,"da_rivedere"),
        ],
    )
    raw_rows = [
        {"Nome":"PayPal","Tipo":"Blocco conto per autorizzazione aperta","Stato":"In sospeso","Lordo":"-4,72","Codice transazione":"HOLD-1","Codice transazione di riferimento":"AUTH-1","Impatto sul saldo":"Addebito"},
        {"Nome":"AMZN Mktp IT","Tipo":"Transazione generica con carta di debito PayPal","Stato":"Completata","Lordo":"-54,99","Codice transazione":"SETTLED-1","Codice transazione di riferimento":"AUTH-1","Impatto sul saldo":"Addebito"},
        {"Nome":"PayPal","Tipo":"Storno di blocco conto generico","Stato":"Completata","Lordo":"4,72","Codice transazione":"REV-1","Codice transazione di riferimento":"HOLD-1","Impatto sul saldo":"Accredito"},
    ]
    conn.executemany(
        "INSERT INTO movements_raw(raw_id,import_id,row_number,payload_originale_json) VALUES(?,?,?,?)",
        [(101 + index, 108, index, json.dumps(payload)) for index, payload in enumerate(raw_rows, start=1)],
    )
    conn.commit()
    conn.close()

    before = sha256(db)
    result = audit_runts_missing_documents(db, year=2025)
    after = sha256(db)

    assert before == after
    assert result["summary"]["raw_expense_movement_count"] == 5
    assert result["summary"]["paypal_pending_authorization_suppressed_count"] == 1
    assert result["summary"]["paypal_pending_authorization_suppressed_total_eur"] == "4.72"
    assert result["summary"]["expense_movement_count"] == 4
    assert result["summary"]["paypal_raw_settled_match_count"] == 1
    assert all(row["movement_id"] != "8" for row in result["rows"])
    settled = next(row for row in result["rows"] if row["movement_id"] == "9")
    assert settled["ready_for_human_confirmation"] is True
    assert settled["paypal_raw_settled_match"] is True
    evidence = next(ev for ev in settled["external_evidence"] if ev["kind"] == "paypal_raw_settled_transaction")
    assert evidence["merchant"] == "AMZN Mktp IT"
    assert evidence["fiscal_document"] is False
    assert evidence["transaction_reference_hash"]

def test_exact_paypal_topup_group_match_never_invents_pair_identity(tmp_path):
    db = tmp_path / "runts_suite.db"
    build_db(db)
    conn = sqlite3.connect(db)
    conn.executemany(
        "INSERT INTO imports(import_id,filename,source_type,import_timestamp,hash_file,parser_usato,account_id) VALUES(?,?,?,?,?,?,?)",
        [
            (108, "bank.xlsx", "xlsx_bank_italia", "2026-01-01T00:00:01", "bank-topup", "fixture", 1),
            (109, "paypal.CSV", "csv_generic", "2026-01-01T00:00:02", "paypal-topup", "fixture", 1),
        ],
    )
    conn.executemany(
        """INSERT INTO movements(
            movement_id,import_id,account_id,data_movimento,descrizione_originale,
            importo_signed,contropartita,category_id,project_id,internal_category_id,
            is_transfer,is_duplicate,movement_kind,notes,stato_validazione
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        [
            (8,108,1,"2025-11-03","Pagamenti paesi UE DEL 29/10/25 C/O PAYPAL *ADD TO BAL CARTA N. 1006",-5,"",5,None,None,0,0,None,None,"da_rivedere"),
            (9,108,1,"2025-11-03","Pagamenti paesi UE DEL 29/10/25 C/O PAYPAL *ADD TO BAL CARTA N. 1006",-5,"",5,None,None,0,0,None,None,"da_rivedere"),
            (10,109,1,"2025-10-29","Versamento generico con carta",5,"",None,None,None,0,0,None,None,"importato"),
            (11,109,1,"2025-10-29","Versamento generico con carta",5,"",None,None,None,0,0,None,None,"importato"),
        ],
    )
    conn.commit()
    conn.close()
    result = audit_runts_missing_documents(db, year=2025)
    matched = [row for row in result["rows"] if row["movement_id"] in {"8","9"}]
    assert len(matched) == 2
    assert all(row["ready_for_human_confirmation"] for row in matched)
    assert all(row["suggested_human_decision"] == "confirm_internal_transfer" for row in matched)
    for row in matched:
        evidence = next(ev for ev in row["external_evidence"] if ev["kind"] == "paypal_balance_transfer_group_match")
        assert evidence["pairing_identity_unresolved"] is True
        assert evidence["candidate_count"] == 2
        assert evidence["debit_group_count"] == 2
        assert evidence["fiscal_document"] is False
    assert result["summary"]["paypal_balance_transfer_group_match_count"] == 2


def test_revolut_completed_payment_raw_evidence_is_ready_but_not_fiscal_document(tmp_path):
    import json
    db = tmp_path / "runts_suite.db"
    build_db(db)
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE movements_raw(raw_id INTEGER PRIMARY KEY, import_id INTEGER NOT NULL, row_number INTEGER NOT NULL, payload_originale_json TEXT NOT NULL)"
    )
    conn.execute(
        "INSERT INTO imports(import_id,filename,source_type,import_timestamp,hash_file,parser_usato,account_id) VALUES(?,?,?,?,?,?,?)",
        (108, "revolut.xlsx", "xlsx_revolut", "2026-01-01T00:00:01", "revolut-hash", "xlsx_revolut_v1", 1),
    )
    conn.execute(
        """INSERT INTO movements(
            movement_id,import_id,account_id,data_movimento,descrizione_originale,
            importo_signed,contropartita,category_id,project_id,internal_category_id,
            is_transfer,is_duplicate,movement_kind,notes,stato_validazione
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (8,108,1,"2025-10-02","To Arci Milano",-135.20,"",5,None,None,0,0,None,None,"da_rivedere"),
    )
    conn.execute(
        "INSERT INTO movements_raw(raw_id,import_id,row_number,payload_originale_json) VALUES(?,?,?,?)",
        (101,108,1,json.dumps({
            "data":"2025-10-02","descrizione":"To Arci Milano","descrizione_banca":"To Arci Milano",
            "importo":"-135.20","Tipo":"Pagamento","State":"COMPLETATO","parser_layout":"revolut_xlsx",
        })),
    )
    conn.commit()
    conn.close()
    result = audit_runts_missing_documents(db, year=2025)
    row = next(item for item in result["rows"] if item["movement_id"] == "8")
    assert row["ready_for_human_confirmation"] is True
    assert row["revolut_completed_payment_match"] is True
    evidence = next(ev for ev in row["external_evidence"] if ev["kind"] == "revolut_completed_payment_reference")
    assert evidence["confidence"] == 95
    assert evidence["fiscal_document"] is False
    assert row["original_document_status"] == "MISSING"
    assert result["summary"]["revolut_completed_payment_match_count"] == 1
