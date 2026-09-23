from __future__ import annotations

import hashlib
import sqlite3

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from openshell_backend import accounting_review_api
from ralfloop_agent.unified_assistant.accounting_review_store import AccountingReviewDecisionStore


def build_review_db(path):
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
        INSERT INTO accounts VALUES (1,'Banca',1,0);
        INSERT INTO categories VALUES (5,'Imposte','uscita');
        INSERT INTO imports VALUES (101,'bank.xlsx','bank','2025-01-01T00:00:01','hash101','fixture',1);
        INSERT INTO movements (
            movement_id,import_id,account_id,data_movimento,descrizione_originale,
            importo_signed,contropartita,category_id,project_id,internal_category_id,
            is_transfer,is_duplicate,movement_kind,notes,stato_validazione
        ) VALUES (
            1,101,1,'2025-06-19',
            'ADDEBITO DELEGA F24 - HB-NET 068 97826900157 ADD.DELEGA F24 HB-NET',
            -1020.06,'Agenzia Entrate',5,NULL,NULL,0,0,NULL,NULL,'da_rivedere'
        );
        """
    )
    conn.commit()
    conn.close()


@pytest.fixture
def client(tmp_path, monkeypatch):
    runts = tmp_path / "runts.sqlite"
    decisions = tmp_path / "decisions.sqlite"
    build_review_db(runts)
    monkeypatch.setenv("BOTTAZZI_RUNTS_DB_PATH", str(runts))
    monkeypatch.setenv("BOTTAZZI_ACCOUNTING_REVIEW_DB", str(decisions))
    monkeypatch.delenv("BOTTAZZI_ACCOUNTING_EVIDENCE_SNAPSHOT", raising=False)
    accounting_review_api._decision_store.cache_clear()
    app = FastAPI()
    app.include_router(accounting_review_api.router, prefix="/assistant/v1")
    yield TestClient(app), runts, decisions
    accounting_review_api._decision_store.cache_clear()


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_review_year_catalog_uses_database_backlog_not_hardcoded_year(client):
    http, _, _ = client
    catalog = http.get("/assistant/v1/accounting/review/years").json()
    assert catalog["default_year"] == 2025
    assert catalog["years"] == [{
        "year": 2025,
        "review_case_count": 1,
        "ready_count": 1,
        "unresolved_count": 0,
        "backlog_count": 1,
    }]
    default_view = http.get("/assistant/v1/accounting/review?view=pending").json()
    assert default_view["year"] == 2025
    assert default_view["total"] == 1


def test_review_api_records_human_decision_without_mutating_runts(client):
    http, runts, decisions = client
    before = sha256(runts)
    pending = http.get("/assistant/v1/accounting/review?year=2025&view=pending").json()
    assert pending["total"] == 1
    assert pending["summary"]["pending_ready_count"] == 1
    row = pending["rows"][0]
    response = http.post(
        "/assistant/v1/accounting/review/decisions",
        json={
            "year": 2025,
            "batch_sha256": pending["batch_sha256"],
            "items": [{
                "movement_id": row["movement_id"],
                "item_sha256": row["item_sha256"],
                "decision": "approve_reconstruction",
            }],
        },
    )
    assert response.status_code == 200
    assert response.json()["runts_writes"] == 0
    assert response.json()["decision_store_writes"] == 1
    assert sha256(runts) == before
    assert decisions.is_file()
    assert http.get("/assistant/v1/accounting/review?year=2025&view=pending").json()["total"] == 0
    decided = http.get("/assistant/v1/accounting/review?year=2025&view=decided").json()
    assert decided["total"] == 1
    assert decided["rows"][0]["current_decision"]["decision"] == "approve_reconstruction"


def test_review_api_fails_closed_on_stale_batch_and_item(client):
    http, _, _ = client
    pending = http.get("/assistant/v1/accounting/review?year=2025&view=pending").json()
    row = pending["rows"][0]
    stale_batch = http.post(
        "/assistant/v1/accounting/review/decisions",
        json={
            "year": 2025,
            "batch_sha256": "0" * 64,
            "items": [{
                "movement_id": row["movement_id"],
                "item_sha256": row["item_sha256"],
                "decision": "reject",
            }],
        },
    )
    assert stale_batch.status_code == 409
    assert stale_batch.json()["detail"] == "accounting_review_batch_stale"
    stale_item = http.post(
        "/assistant/v1/accounting/review/decisions",
        json={
            "year": 2025,
            "batch_sha256": pending["batch_sha256"],
            "items": [{
                "movement_id": row["movement_id"],
                "item_sha256": "f" * 64,
                "decision": "reject",
            }],
        },
    )
    assert stale_item.status_code == 409
    assert stale_item.json()["detail"] == "accounting_review_item_stale"


def test_transfer_decision_requires_transfer_evidence(client):
    http, _, _ = client
    pending = http.get("/assistant/v1/accounting/review?year=2025&view=pending").json()
    row = pending["rows"][0]
    response = http.post(
        "/assistant/v1/accounting/review/decisions",
        json={
            "year": 2025,
            "batch_sha256": pending["batch_sha256"],
            "items": [{
                "movement_id": row["movement_id"],
                "item_sha256": row["item_sha256"],
                "decision": "confirm_internal_transfer",
            }],
        },
    )
    assert response.status_code == 400
    assert response.json()["detail"] == "accounting_review_transfer_evidence_missing"


def test_decision_store_keeps_revision_history(tmp_path):
    store = AccountingReviewDecisionStore(tmp_path / "review.sqlite")
    item = {"movement_id": "7", "item_sha256": "a" * 64, "decision": "approve_reconstruction"}
    first = store.apply_many(year=2025, batch_sha256="b" * 64, items=[item])[0]
    second = store.apply_many(
        year=2025,
        batch_sha256="b" * 64,
        items=[{**item, "decision": "reject"}],
    )[0]
    assert first["revision"] == 1
    assert second["revision"] == 2
    assert store.list_year(2025)["7"]["decision"] == "reject"
    assert [row["decision"] for row in store.history(2025, "7")] == [
        "approve_reconstruction", "reject"
    ]
