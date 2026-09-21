from __future__ import annotations

import json
from pathlib import Path

import pytest

from ralfloop_agent.unified_assistant.md_goodify_transactional import (
    FlowStore,
    GoodifyProtocolError,
    MdGoodifyFlow,
    MdPurchaseAmbiguous,
    MdPurchaseRejected,
    parse_goodify_donation_id,
    parse_goodify_redirect_id,
    qr_fingerprint,
)

DONATION_ID = "donation_ABC12345"
DONATION_URL = f"https://me.goodify.com/donation/{DONATION_ID}"
RECIPIENT = {"id": "recipient-tiremm", "name": "TIREMM INNANZ APS", "verified": True}


class FakePurchase:
    def __init__(self, *, fail: Exception | None = None):
        self.calls = 0
        self.fail = fail

    def purchase(self, qr_code: str):
        self.calls += 1
        if self.fail:
            raise self.fail
        assert qr_code == "QR-REAL-123"
        return {"donation_id": DONATION_ID, "donation_url": DONATION_URL}


class FakeGraphQL:
    def __init__(self, *, win=25, initially_tiremm=False, change_error=False, instant=True):
        self.win = win
        self.initially_tiremm = initially_tiremm
        self.change_error = change_error
        self.instant = instant
        self.change_calls = 0
        self.verify_calls = 0
        self.get_calls = 0
        self.changed = initially_tiremm

    def resolve_tiremm_recipient(self):
        return dict(RECIPIENT)

    def get_donation(self, donation_id: str):
        self.get_calls += 1
        assert donation_id == DONATION_ID
        recipient = {"id": RECIPIENT["id"], "name": RECIPIENT["name"]} if self.changed else None
        return {"id": donation_id, "recipient": recipient, "status": "CREATED", "recipientType": "MANUAL",
                "campaign": {"id": "campaign-1", "instantWinIntegration": self.instant}}

    def redeem_redirect(self, redirect_id: str):
        assert redirect_id == "redirect_REAL_123"
        return DONATION_ID

    def change_recipient(self, donation_id: str, recipient_id: str):
        self.change_calls += 1
        assert donation_id == DONATION_ID
        assert recipient_id == RECIPIENT["id"]
        self.changed = True
        if self.change_error:
            raise GoodifyProtocolError("connection_lost_after_mutation")

    def verify_instant_win(self, donation_id: str):
        self.verify_calls += 1
        assert donation_id == DONATION_ID
        return self.win


def test_parse_goodify_donation_id_is_strict():
    assert parse_goodify_donation_id(DONATION_URL) == DONATION_ID
    assert parse_goodify_donation_id(DONATION_URL + "?x=1") == DONATION_ID
    with pytest.raises(GoodifyProtocolError, match="host"):
        parse_goodify_donation_id("https://evilgoodify.com/donation/donation_ABC12345")
    with pytest.raises(GoodifyProtocolError, match="missing"):
        parse_goodify_donation_id("https://me.goodify.com/other/donation_ABC12345")


def test_qr_state_stores_only_hash_and_length(tmp_path: Path):
    qr, fingerprint = qr_fingerprint(" QR-REAL-123 ")
    assert qr == "QR-REAL-123"
    store = FlowStore(tmp_path / "transactions.sqlite3")
    is_new, row = store.reserve(fingerprint, len(qr))
    assert is_new is True
    assert row["phase"] == "RECEIVED"
    raw = (tmp_path / "transactions.sqlite3").read_bytes()
    assert b"QR-REAL-123" not in raw


def test_end_to_end_is_idempotent_and_queues_win_once(tmp_path: Path):
    purchase = FakePurchase()
    gql = FakeGraphQL(win=25)
    flow = MdGoodifyFlow(purchase_client=purchase, graphql_client=gql,
                         store=FlowStore(tmp_path / "state.sqlite3"),
                         telegram_outbox=tmp_path / "telegram.jsonl")

    first = flow.process_qr("QR-REAL-123")
    assert first["ok"] is True
    assert first["status"] == "DONATED_TO_TIREMM"
    assert first["recipient"]["name"] == "TIREMM INNANZ APS"
    assert first["instant_win"] == {"status": "WIN", "amount": 25}
    assert purchase.calls == 1
    assert gql.change_calls == 1
    assert gql.verify_calls == 1

    second = flow.process_qr("QR-REAL-123")
    assert second["already_processed"] is True
    assert purchase.calls == 1
    assert gql.change_calls == 1
    assert gql.verify_calls == 1
    rows = (tmp_path / "telegram.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(rows) == 1
    assert json.loads(rows[0])["kind"] == "md_goodify_win_notification"


def test_recipient_mutation_network_error_is_reconciled_by_postcondition(tmp_path: Path):
    purchase = FakePurchase()
    gql = FakeGraphQL(change_error=True, win=None)
    flow = MdGoodifyFlow(purchase_client=purchase, graphql_client=gql,
                         store=FlowStore(tmp_path / "state.sqlite3"), telegram_outbox=tmp_path / "out.jsonl")
    result = flow.process_qr("QR-REAL-123")
    assert result["ok"] is True
    assert result["recipient"]["id"] == RECIPIENT["id"]
    assert result["instant_win"]["status"] == "LOSS"
    assert gql.change_calls == 1


def test_definite_md_rejection_never_retries_same_qr(tmp_path: Path):
    purchase = FakePurchase(fail=MdPurchaseRejected("md_goodify_purchase_rejected"))
    gql = FakeGraphQL()
    flow = MdGoodifyFlow(purchase_client=purchase, graphql_client=gql,
                         store=FlowStore(tmp_path / "state.sqlite3"), telegram_outbox=tmp_path / "out.jsonl")
    first = flow.process_qr("QR-REAL-123")
    second = flow.process_qr("QR-REAL-123")
    assert first["status"] == "MD_REJECTED"
    assert second["status"] == "MD_REJECTED"
    assert second["already_processed"] is True
    assert purchase.calls == 1


def test_uncertain_purchase_is_never_resubmitted(tmp_path: Path):
    purchase = FakePurchase(fail=OSError("socket reset"))
    gql = FakeGraphQL()
    flow = MdGoodifyFlow(purchase_client=purchase, graphql_client=gql,
                         store=FlowStore(tmp_path / "state.sqlite3"), telegram_outbox=tmp_path / "out.jsonl")
    first = flow.process_qr("QR-REAL-123")
    second = flow.process_qr("QR-REAL-123")
    assert first["status"] == "AMBIGUOUS_PURCHASE"
    assert second["status"] == "AMBIGUOUS_PURCHASE"
    assert second["already_processed"] is True
    assert purchase.calls == 1


def test_no_instant_win_campaign_skips_verify(tmp_path: Path):
    purchase = FakePurchase()
    gql = FakeGraphQL(instant=False)
    flow = MdGoodifyFlow(purchase_client=purchase, graphql_client=gql,
                         store=FlowStore(tmp_path / "state.sqlite3"), telegram_outbox=tmp_path / "out.jsonl")
    result = flow.process_qr("QR-REAL-123")
    assert result["instant_win"]["status"] == "NOT_AVAILABLE"
    assert gql.verify_calls == 0


def test_concurrent_duplicate_is_processing_without_second_side_effect(tmp_path: Path):
    purchase = FakePurchase()
    gql = FakeGraphQL()
    store = FlowStore(tmp_path / "state.sqlite3")
    _, fingerprint = qr_fingerprint("QR-REAL-123")
    is_new, _ = store.reserve(fingerprint, len("QR-REAL-123"))
    assert is_new is True

    flow = MdGoodifyFlow(purchase_client=purchase, graphql_client=gql,
                         store=store, telegram_outbox=tmp_path / "out.jsonl")
    result = flow.process_qr("QR-REAL-123")
    assert result["status"] == "PROCESSING"
    assert result["already_processed"] is True
    assert purchase.calls == 0
    assert gql.change_calls == 0
    assert gql.verify_calls == 0


def test_refresh_failure_releases_qr_without_purchase_side_effect(tmp_path: Path):
    class PreflightFailPurchase(FakePurchase):
        def preflight(self):
            raise OSError("refresh unavailable")

    purchase = PreflightFailPurchase()
    gql = FakeGraphQL()
    store = FlowStore(tmp_path / "state.sqlite3")
    flow = MdGoodifyFlow(purchase_client=purchase, graphql_client=gql,
                         store=store, telegram_outbox=tmp_path / "out.jsonl")
    result = flow.process_qr("QR-REAL-123")
    assert result["status"] == "AUTH_REFRESH_FAILED"
    assert purchase.calls == 0
    _, fingerprint = qr_fingerprint("QR-REAL-123")
    assert store.get(fingerprint) is None


class FakeHistory:
    def __init__(self, snapshots):
        self.snapshots = list(snapshots)
        self.calls = 0

    def get_donations(self):
        index = min(self.calls, len(self.snapshots) - 1)
        self.calls += 1
        return {"donations": list(self.snapshots[index])}


def test_parse_goodify_view_redirect_is_strict():
    url = "https://me.goodify.com/view/redirect_REAL_123"
    assert parse_goodify_redirect_id(url) == "redirect_REAL_123"
    with pytest.raises(GoodifyProtocolError, match="host"):
        parse_goodify_redirect_id("https://evilgoodify.com/view/redirect_REAL_123")
    with pytest.raises(GoodifyProtocolError, match="redirect_id_missing"):
        parse_goodify_redirect_id("https://me.goodify.com/donation/redirect_REAL_123")


def test_view_redirect_is_redeemed_before_recipient_change(tmp_path: Path):
    class RedirectPurchase(FakePurchase):
        def purchase(self, qr_code: str):
            self.calls += 1
            assert qr_code == "QR-REAL-123"
            return {
                "redirect_id": "redirect_REAL_123",
                "donation_url": "https://me.goodify.com/view/redirect_REAL_123",
            }

    purchase = RedirectPurchase()
    gql = FakeGraphQL(win=None)
    flow = MdGoodifyFlow(
        purchase_client=purchase,
        graphql_client=gql,
        store=FlowStore(tmp_path / "state.sqlite3"),
        telegram_outbox=tmp_path / "out.jsonl",
    )
    result = flow.process_qr("QR-REAL-123")
    assert result["ok"] is True
    assert result["donation_id"] == DONATION_ID
    assert result["recipient"]["name"] == "TIREMM INNANZ APS"
    assert purchase.calls == 1
    assert gql.change_calls == 1


def test_uncertain_purchase_reconciles_single_new_md_history_row(tmp_path: Path):
    purchase = FakePurchase(fail=MdPurchaseAmbiguous("socket reset"))
    gql = FakeGraphQL(win=None)
    before = [{
        "Goodify_id": "100",
        "Goodify_donationId": "old_redirect_123",
        "Goodify_UrldonationId": "https://me.goodify.com/view/old_redirect_123",
    }]
    after = before + [{
        "Goodify_id": "101",
        "Goodify_donationId": "redirect_REAL_123",
        "Goodify_UrldonationId": "https://me.goodify.com/view/redirect_REAL_123",
    }]
    history = FakeHistory([before, after])
    flow = MdGoodifyFlow(
        purchase_client=purchase,
        graphql_client=gql,
        history_client=history,
        store=FlowStore(tmp_path / "state.sqlite3"),
        telegram_outbox=tmp_path / "out.jsonl",
    )
    result = flow.process_qr("QR-REAL-123")
    assert result["status"] == "DONATED_TO_TIREMM"
    assert result["donation_id"] == DONATION_ID
    assert purchase.calls == 1
    assert history.calls == 2
