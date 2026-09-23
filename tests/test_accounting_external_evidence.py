from __future__ import annotations

from contextlib import AbstractContextManager

from ralfloop_agent.unified_assistant.accounting_external_evidence import (
    collect_paypal_receipts,
    enrich_rows_with_paypal,
    match_paypal_receipt,
    merchant_similarity,
    parse_paypal_receipt,
)


BODY = """
Fabio Fagioli, ecco il tuo codice transazione: 171048369D8122941.
Hai pagato 189,67 € EUR a Klarna*Rinnovato.it
Grazie per aver usato la tua PayPal Business Debit Mastercard®
le cui ultime cifre sono 2055.
"""


def test_parse_paypal_receipt_extracts_payment_evidence_not_fiscal_document():
    row = parse_paypal_receipt({
        "messageId": "abc123",
        "date": "Wed, 07 May 2025 21:17:35 -0700",
        "from": "assistenza@paypal.it",
        "subject": "Ricevuta della tua transazione recente",
        "body": BODY,
    })
    assert row is not None
    assert row["amount_eur"] == "189.67"
    assert row["merchant"] == "Klarna*Rinnovato.it"
    assert row["transaction_id"] == "171048369D8122941"
    assert row["card_suffix"] == "2055"
    assert row["date"] == "2025-05-07"
    assert row["fiscal_document"] is False
    assert row["content_hash"]


def test_merchant_similarity_handles_payment_prefixes():
    assert merchant_similarity("Klarna*Rinnovato.it", "Klarna*Rinnovato.it") == 1.0
    assert merchant_similarity("Amazon.it", "Amazon.it") == 1.0
    assert merchant_similarity("Completely Different", "Amazon.it") == 0.0


def test_unique_paypal_receipt_match_requires_amount_date_and_merchant():
    receipt = parse_paypal_receipt({
        "messageId": "abc123",
        "date": "Wed, 07 May 2025 21:17:35 -0700",
        "from": "assistenza@paypal.it",
        "subject": "Ricevuta",
        "body": BODY,
    })
    movement = {
        "movement_id": "487",
        "movement_date": "2025-05-08",
        "amount_eur": "189.67",
        "counterparty": "Klarna*Rinnovato.it",
        "description": "Klarna*Rinnovato.it",
    }
    match = match_paypal_receipt(movement, [receipt])
    assert match["status"] == "UNIQUE_MATCH"
    assert match["confidence"] == 95
    assert match["date_distance_days"] == 1
    assert match["fiscal_document"] is False
    assert match_paypal_receipt({**movement, "amount_eur": "190.00"}, [receipt]) is None


def test_enrichment_marks_case_ready_for_human_confirmation_without_approving_it():
    receipt = parse_paypal_receipt({
        "messageId": "abc123",
        "date": "Wed, 07 May 2025 21:17:35 -0700",
        "from": "assistenza@paypal.it",
        "subject": "Ricevuta",
        "body": BODY,
    })
    rows = enrich_rows_with_paypal([
        {
            "movement_id": "487",
            "movement_date": "2025-05-08",
            "amount_eur": "189.67",
            "counterparty": "Klarna*Rinnovato.it",
            "description": "Klarna*Rinnovato.it",
            "original_document_status": "MISSING",
            "accounting_status": "REVIEW_REQUIRED",
            "human_review_required": True,
            "reconstruction_evidence_score": 40,
        }
    ], {"receipts": [receipt]})
    row = rows[0]
    assert row["ready_for_human_confirmation"] is True
    assert row["human_review_required"] is True
    assert row["accounting_status"] == "REVIEW_REQUIRED"
    assert row["external_evidence"][0]["kind"] == "paypal_email_receipt"
    assert row["external_evidence"][0]["fiscal_document"] is False


def test_bank_native_references_are_strong_payment_context_not_fiscal_documents():
    from ralfloop_agent.unified_assistant.accounting_external_evidence import extract_bank_native_reference
    f24 = extract_bank_native_reference({"movement_id":"2393","description":"ADDEBITO DELEGA F24 - HB-NET 068 97826900157 ADD.DELEGA F24 HB-NET"})
    assert f24["kind"] == "bank_f24_reference" and f24["confidence"] == 95
    assert f24["fiscal_document"] is False and f24["native_reference_hash"]
    cbill = extract_bank_native_reference({"movement_id":"2422","description":"PAGAMENTO UTENZA PAG.TO CBILL DI EURO 149,00 BILLER: A0EDT N.DOCUMENTO 002000006790540345"})
    assert cbill["kind"] == "bank_cbill_reference" and cbill["document_reference"] == "002000006790540345"
    transfer = extract_bank_native_reference({"movement_id":"2408","description":"ADDEBITO BONIFICO DA HOME BANKING john north Cro: 0000028226873811480160001600IT FATTURA N 2 2025 LEZIONI DI INGLESE"})
    assert transfer["kind"] == "bank_transfer_reference" and transfer["confidence"] == 92
    fee = extract_bank_native_reference({"movement_id":"2447","description":"COMPETENZE SPESE"})
    assert fee["kind"] == "bank_fee_statement_reference" and fee["confidence"] == 95
    assert fee["fiscal_document"] is False
    sdd = extract_bank_native_reference({"movement_id":"2500","description":"PAGAMENTO UTENZA TELEFONICA CORE RCUR Prg.Car.: 250240490021197 FASTWEB SPA - ADDEBITO FASTWEB 2025- M003048569 SDD 11662640"})
    assert sdd["kind"] == "bank_sdd_utility_reference" and sdd["confidence"] == 92
    card = extract_bank_native_reference({"movement_id":"2465","description":"Pagamenti paesi UE DEL 07/03/25 IN ITALIA A MILANO Valuta EUR Paese Italia C/O JustEatItaly CARTA N. 483847******1006 - CIRCUITO VISA"})
    assert card["kind"] == "bank_card_merchant_reference" and card["confidence"] == 90
    topup = extract_bank_native_reference({"movement_id":"2504","description":"Pagamenti paesi UE DEL 20/01/25 IN ITALIA C/O PAYPAL *ADD TO BAL CARTA N. 483847******1006 - CIRCUITO VISA"})
    assert topup is None
    assert extract_bank_native_reference({"movement_id":"1","description":"Prelievo Con Bonifico"}) is None


def test_bank_native_enrichment_marks_only_strong_references_ready_for_human_confirmation():
    from ralfloop_agent.unified_assistant.accounting_external_evidence import enrich_rows_with_bank_native
    rows = enrich_rows_with_bank_native([
        {"movement_id":"1","description":"ADDEBITO DIRETTO CORE RCUR HERA S.P.A.","ready_for_human_confirmation":False,"reconstruction_evidence_score":20,"external_evidence":[]},
        {"movement_id":"2","description":"Prelievo Con Bonifico","ready_for_human_confirmation":False,"reconstruction_evidence_score":20,"external_evidence":[]},
    ])
    assert rows[0]["ready_for_human_confirmation"] is True
    assert rows[0]["external_evidence"][0]["kind"] == "bank_direct_debit_reference"
    assert rows[0]["external_evidence"][0]["fiscal_document"] is False
    assert rows[1]["ready_for_human_confirmation"] is False


class FakeGateway:
    def __init__(self):
        self.search_calls = 0
        self.read_calls = 0

    def invoke(self, operation, **arguments):
        if operation == "search":
            self.search_calls += 1
            if "after:2025/05/01" in arguments["query"]:
                return {"messages": [{"messageId": "abc123"}]}
            return {"messages": []}
        if operation == "read":
            self.read_calls += 1
            return {"message": {
                "messageId": "abc123",
                "date": "Wed, 07 May 2025 21:17:35 -0700",
                "from": "assistenza@paypal.it",
                "subject": "Ricevuta della tua transazione recente",
                "body": BODY,
            }}
        raise AssertionError(operation)


class FakeContext(AbstractContextManager):
    def __init__(self, gateway):
        self.gateway = gateway

    def __enter__(self):
        return self.gateway

    def __exit__(self, exc_type, exc, tb):
        return None


def test_collect_paypal_receipts_partitions_by_month_and_never_writes():
    gateway = FakeGateway()
    snapshot = collect_paypal_receipts(
        year=2025,
        context_factory=lambda: FakeContext(gateway),
    )
    assert gateway.search_calls == 12
    assert gateway.read_calls == 1
    assert snapshot["receipt_count"] == 1
    assert snapshot["receipts"][0]["provenance_ref"] == "gmail:message:abc123"
    assert snapshot["writes"] == 0
    assert snapshot["sends"] == 0


def test_human_confirmation_batch_is_hash_bound_and_nonexecuting():
    from ralfloop_agent.unified_assistant.accounting_review import build_human_confirmation_batch
    rows = [{"movement_id":"1","movement_date":"2025-01-02","amount_eur":"10.00","counterparty":"Shop","description":"Shop","ready_for_human_confirmation":True,"original_document_status":"MISSING","human_review_required":True,"external_evidence":[{"kind":"paypal_email_receipt","provenance_ref":"gmail:message:abc","confidence":95,"fiscal_document":False}]}]
    batch = build_human_confirmation_batch(rows, year=2025)
    assert batch["item_count"] == 1
    assert batch["total_eur"] == "10.00"
    assert batch["requires_human_approval"] is True
    assert batch["executable"] is False
    assert batch["writes"] == 0
    assert batch["batch_sha256"] and batch["evidence_refs"] == ["gmail:message:abc"]


def test_same_external_evidence_cannot_auto_support_two_movements():
    from ralfloop_agent.unified_assistant.accounting_external_evidence import enforce_unique_external_evidence
    ev={"kind":"paypal_email_receipt","provenance_ref":"gmail:message:same","confidence":95,"fiscal_document":False}
    rows=[{"movement_id":"1","ready_for_human_confirmation":True,"reconstruction_evidence_score":95,"external_evidence":[ev]},{"movement_id":"2","ready_for_human_confirmation":True,"reconstruction_evidence_score":95,"external_evidence":[ev]}]
    guarded=enforce_unique_external_evidence(rows)
    assert not guarded[0]["ready_for_human_confirmation"] and not guarded[1]["ready_for_human_confirmation"]
    assert guarded[0]["external_evidence_conflict"] == ["gmail:message:same"]
    assert guarded[0]["reconstruction_evidence_score"] == 60
