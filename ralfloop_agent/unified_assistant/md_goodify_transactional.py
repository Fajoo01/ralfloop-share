from __future__ import annotations

"""Guarded end-to-end MD -> Goodify donation flow.

Secrets stay server-side. The flow accepts only a user-supplied MD QR, resolves the
fixed Tiremm recipient through Goodify, persists an idempotency state machine, and
never stores the raw QR or MD access token in its state database.
"""

from dataclasses import dataclass
import hashlib
import http.client
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import time
from typing import Any, Callable, Mapping
from urllib.parse import urlparse

from .md_goodify_api import build_purchase_donation_body, parse_goodify_response
from .md_goodify_readonly import load_access_token

MD_GOODIFY_HOST = "catalogomdapp.dedagroupwiz.it"
MD_PURCHASE_PATH = "/api/goodify/purchasedonation"
GRAPHQL_HOST = "api.goodify.com"
GRAPHQL_PATH = "/graphql"
DEFAULT_STATE_DB = Path("/var/lib/ralfloop/md-goodify/transactions.sqlite3")
DEFAULT_TELEGRAM_OUTBOX = Path("/var/lib/ralfloop/domain-approval-outbox.jsonl")
EXPECTED_RECIPIENT = os.getenv("RALFLOOP_MD_GOODIFY_RECIPIENT_CANONICAL", "TIREMM INNANZ APS").strip()
MAX_RESPONSE_BYTES = 1024 * 1024
DONATION_ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,160}$")
ConnectionFactory = Callable[..., http.client.HTTPSConnection]


class MdPurchaseRejected(RuntimeError):
    """A definite MD application-level rejection; safe to report without retrying."""


class GoodifyProtocolError(RuntimeError):
    pass


def _json_bytes(value: Mapping[str, Any]) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _read_json_response(response: Any) -> Mapping[str, Any]:
    raw = response.read(MAX_RESPONSE_BYTES + 1)
    if len(raw) > MAX_RESPONSE_BYTES:
        raise GoodifyProtocolError("goodify_response_too_large")
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise GoodifyProtocolError("goodify_invalid_json") from exc
    if not isinstance(value, Mapping):
        raise GoodifyProtocolError("goodify_response_not_object")
    return value


def qr_fingerprint(qr_code: str) -> tuple[str, str]:
    qr = str(qr_code or "").strip()
    if not qr or len(qr) > 4096:
        raise ValueError("md_goodify_invalid_qr_code")
    return qr, hashlib.sha256(qr.encode("utf-8")).hexdigest()


def parse_goodify_donation_id(url: str) -> str:
    parsed = urlparse(str(url or "").strip())
    host = str(parsed.hostname or "").casefold().rstrip(".")
    if parsed.scheme.casefold() != "https" or not host:
        raise GoodifyProtocolError("goodify_invalid_donation_url")
    if not (host == "goodify.com" or host.endswith(".goodify.com")):
        raise GoodifyProtocolError("goodify_invalid_donation_host")
    if parsed.username or parsed.password or parsed.port not in (None, 443):
        raise GoodifyProtocolError("goodify_invalid_donation_authority")
    parts = [part for part in parsed.path.split("/") if part]
    try:
        index = parts.index("donation")
        donation_id = parts[index + 1]
    except (ValueError, IndexError) as exc:
        raise GoodifyProtocolError("goodify_donation_id_missing") from exc
    if not DONATION_ID_RE.fullmatch(donation_id):
        raise GoodifyProtocolError("goodify_donation_id_invalid")
    return donation_id


class MdPurchaseClient:
    def __init__(self, *, token_path: Path | None = None, timeout: float = 12.0,
                 connection_factory: ConnectionFactory = http.client.HTTPSConnection) -> None:
        self.token_path = token_path
        self.timeout = timeout
        self.connection_factory = connection_factory

    def purchase(self, qr_code: str) -> dict[str, Any]:
        token = load_access_token(self.token_path)
        body = _json_bytes(build_purchase_donation_body(token, qr_code))
        connection = self.connection_factory(MD_GOODIFY_HOST, 443, timeout=self.timeout)
        try:
            connection.request("POST", MD_PURCHASE_PATH, body=body, headers={
                "Content-Type": "application/json", "Accept": "application/json"
            })
            response = connection.getresponse()
            value = _read_json_response(response)
        finally:
            connection.close()
        if response.status < 200 or response.status >= 300:
            raise MdPurchaseRejected(f"md_goodify_http_{response.status}")
        parsed = parse_goodify_response(value)
        if parsed.code:
            raise MdPurchaseRejected("md_goodify_purchase_rejected")
        donation = parsed.first_donation
        if not donation:
            raise MdPurchaseRejected("md_goodify_purchase_missing_donation")
        donation_url = str(donation.get("Goodify_UrldonationId") or "").strip()
        donation_id = parse_goodify_donation_id(donation_url)
        return {"donation_id": donation_id, "donation_url": donation_url}


class GoodifyGraphQLClient:
    def __init__(self, *, timeout: float = 12.0,
                 connection_factory: ConnectionFactory = http.client.HTTPSConnection) -> None:
        self.timeout = timeout
        self.connection_factory = connection_factory

    def _post(self, query: str, variables: Mapping[str, Any]) -> Mapping[str, Any]:
        connection = self.connection_factory(GRAPHQL_HOST, 443, timeout=self.timeout)
        try:
            connection.request("POST", GRAPHQL_PATH, body=_json_bytes({
                "query": query, "variables": dict(variables)
            }), headers={"Content-Type": "application/json", "Accept": "application/json"})
            response = connection.getresponse()
            value = _read_json_response(response)
        finally:
            connection.close()
        if response.status < 200 or response.status >= 300:
            raise GoodifyProtocolError(f"goodify_graphql_http_{response.status}")
        errors = value.get("errors")
        if errors:
            raise GoodifyProtocolError("goodify_graphql_error")
        data = value.get("data")
        if not isinstance(data, Mapping):
            raise GoodifyProtocolError("goodify_graphql_missing_data")
        return data

    def resolve_tiremm_recipient(self, expected_name: str = EXPECTED_RECIPIENT) -> dict[str, Any]:
        data = self._post(
            "query GetNonProfits($search:String,$limit:Int){"
            "getNonProfits(search:$search,limit:$limit){id name verified}}",
            {"search": expected_name, "limit": 20},
        )
        rows = data.get("getNonProfits")
        if not isinstance(rows, list):
            raise GoodifyProtocolError("goodify_recipient_search_invalid")
        exact = [row for row in rows if isinstance(row, Mapping)
                 and row.get("name") == expected_name and row.get("verified") is True]
        if len(exact) != 1 or not str(exact[0].get("id") or "").strip():
            raise GoodifyProtocolError("goodify_tiremm_recipient_not_unique")
        return {"id": str(exact[0]["id"]), "name": str(exact[0]["name"]), "verified": True}

    def get_donation(self, donation_id: str) -> dict[str, Any]:
        data = self._post(
            "query GetDonation($id:ID!){getDonation(id:$id){id status recipientType "
            "recipient{id name} campaign{id instantWinIntegration}}}",
            {"id": donation_id},
        )
        row = data.get("getDonation")
        if not isinstance(row, Mapping) or str(row.get("id") or "") != donation_id:
            raise GoodifyProtocolError("goodify_donation_not_found")
        return dict(row)

    def change_recipient(self, donation_id: str, recipient_id: str) -> None:
        data = self._post(
            "mutation changeDonationRecipient($donationId:ID!,$recipientId:ID,$isRandom:Boolean,$language:LanguageCode){"
            "changeDonationRecipient(donationId:$donationId,recipientId:$recipientId,isRandom:$isRandom,language:$language)}",
            {"donationId": donation_id, "recipientId": recipient_id, "isRandom": False, "language": "it"},
        )
        if data.get("changeDonationRecipient") is not True:
            raise GoodifyProtocolError("goodify_recipient_change_not_confirmed")

    def verify_instant_win(self, donation_id: str) -> Any:
        data = self._post(
            "mutation VerifyInstantWin($donationId:ID!){verifyInstantWin(donationId:$donationId)}",
            {"donationId": donation_id},
        )
        return data.get("verifyInstantWin")


def _private_db(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    connection = sqlite3.connect(path, timeout=30.0)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=DELETE")
    connection.execute("PRAGMA synchronous=FULL")
    connection.execute(
        "CREATE TABLE IF NOT EXISTS qr_flow ("
        "fingerprint TEXT PRIMARY KEY, qr_len INTEGER NOT NULL, phase TEXT NOT NULL, "
        "donation_id TEXT, donation_url TEXT, result_json TEXT, created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL)"
    )
    connection.commit()
    os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    return connection


@dataclass
class FlowStore:
    path: Path = DEFAULT_STATE_DB

    def reserve(self, fingerprint: str, qr_len: int) -> tuple[bool, dict[str, Any]]:
        now = int(time.time())
        with _private_db(self.path) as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM qr_flow WHERE fingerprint=?", (fingerprint,)).fetchone()
            if row is not None:
                db.commit()
                return False, dict(row)
            db.execute(
                "INSERT INTO qr_flow(fingerprint,qr_len,phase,created_at,updated_at) VALUES(?,?,?,?,?)",
                (fingerprint, qr_len, "RECEIVED", now, now),
            )
            db.commit()
            return True, {"fingerprint": fingerprint, "qr_len": qr_len, "phase": "RECEIVED",
                          "created_at": now, "updated_at": now}

    def update(self, fingerprint: str, phase: str, *, donation_id: str | None = None,
               donation_url: str | None = None, result: Mapping[str, Any] | None = None) -> None:
        now = int(time.time())
        result_json = None if result is None else json.dumps(dict(result), ensure_ascii=False, sort_keys=True)
        with _private_db(self.path) as db:
            db.execute("BEGIN IMMEDIATE")
            current = db.execute("SELECT donation_id,donation_url,result_json FROM qr_flow WHERE fingerprint=?",
                                 (fingerprint,)).fetchone()
            if current is None:
                raise RuntimeError("md_goodify_state_missing")
            db.execute(
                "UPDATE qr_flow SET phase=?, donation_id=?, donation_url=?, result_json=?, updated_at=? WHERE fingerprint=?",
                (phase, donation_id if donation_id is not None else current[0],
                 donation_url if donation_url is not None else current[1],
                 result_json if result is not None else current[2], now, fingerprint),
            )
            db.commit()

    def get(self, fingerprint: str) -> dict[str, Any] | None:
        with _private_db(self.path) as db:
            row = db.execute("SELECT * FROM qr_flow WHERE fingerprint=?", (fingerprint,)).fetchone()
            return dict(row) if row is not None else None


def _recipient_is_tiremm(donation: Mapping[str, Any], recipient: Mapping[str, Any]) -> bool:
    current = donation.get("recipient")
    return isinstance(current, Mapping) and current.get("id") == recipient.get("id") and current.get("name") == recipient.get("name")


def _instant_result(value: Any) -> dict[str, Any]:
    if value:
        return {"status": "WIN", "amount": value}
    return {"status": "LOSS", "amount": None}


def _queue_win(donation_id: str, value: Any, outbox: Path) -> str:
    request_id = "mdgoodify_api_" + hashlib.sha256(donation_id.encode("utf-8")).hexdigest()[:24]
    outbox.parent.mkdir(parents=True, exist_ok=True)
    row = {"status": "queued", "kind": "md_goodify_win_notification", "request_id": request_id,
           "created_at": int(time.time()), "message": f"Bot-tazzi — Goodify: vincita rilevata. Premio: {value}."}
    with outbox.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    return request_id


class MdGoodifyFlow:
    def __init__(self, *, purchase_client: Any | None = None, graphql_client: Any | None = None,
                 store: FlowStore | None = None, telegram_outbox: Path = DEFAULT_TELEGRAM_OUTBOX,
                 stale_after: int = 180) -> None:
        self.purchase_client = purchase_client or MdPurchaseClient()
        self.graphql = graphql_client or GoodifyGraphQLClient()
        self.store = store or FlowStore()
        self.telegram_outbox = telegram_outbox
        self.stale_after = stale_after

    def _stored_result(self, row: Mapping[str, Any]) -> dict[str, Any] | None:
        raw = row.get("result_json")
        if not raw:
            return None
        try:
            value = json.loads(str(raw))
        except json.JSONDecodeError:
            return None
        if isinstance(value, dict):
            value["already_processed"] = True
            return value
        return None

    def _finish(self, fingerprint: str, result: dict[str, Any], phase: str = "COMPLETE") -> dict[str, Any]:
        self.store.update(fingerprint, phase, result=result)
        return result

    def process_qr(self, qr_code: str) -> dict[str, Any]:
        qr, fingerprint = qr_fingerprint(qr_code)
        is_new, row = self.store.reserve(fingerprint, len(qr))
        stored = self._stored_result(row)
        if stored is not None:
            return stored

        phase = str(row.get("phase") or "RECEIVED")
        age = int(time.time()) - int(row.get("updated_at") or 0)
        donation_id = str(row.get("donation_id") or "")
        donation_url = str(row.get("donation_url") or "")

        active_phases = {
            "RECEIVED", "PURCHASE_SUBMITTING", "PURCHASED",
            "RECIPIENT_SUBMITTING", "RECIPIENT_SET", "INSTANT_WIN_SUBMITTING",
        }
        if not is_new and phase in active_phases and age < self.stale_after:
            return {"ok": True, "status": "PROCESSING", "already_processed": True,
                    "qr_fingerprint": fingerprint}

        if not is_new and phase == "PURCHASE_SUBMITTING":
            result = {"ok": False, "status": "AMBIGUOUS_PURCHASE", "qr_fingerprint": fingerprint,
                      "message": "Il QR non viene reinviato automaticamente: l'esito del precedente POST MD è incerto."}
            return self._finish(fingerprint, result, "AMBIGUOUS")
        if not is_new and phase == "INSTANT_WIN_SUBMITTING":
            result = {"ok": True, "status": "DONATED_TO_TIREMM", "qr_fingerprint": fingerprint,
                      "donation_id": donation_id, "donation_url": donation_url,
                      "instant_win": {"status": "UNKNOWN", "amount": None},
                      "message": "Donazione confermata; esito instant win affidato al watcher email per evitare un secondo tentativo."}
            return self._finish(fingerprint, result, "COMPLETE_WIN_UNKNOWN")

        recipient = self.graphql.resolve_tiremm_recipient()

        if not donation_id:
            self.store.update(fingerprint, "PURCHASE_SUBMITTING")
            try:
                purchased = self.purchase_client.purchase(qr)
            except MdPurchaseRejected as exc:
                result = {"ok": False, "status": "MD_REJECTED", "qr_fingerprint": fingerprint, "message": str(exc)}
                return self._finish(fingerprint, result, "FAILED_SAFE")
            except Exception:
                result = {"ok": False, "status": "AMBIGUOUS_PURCHASE", "qr_fingerprint": fingerprint,
                          "message": "Connessione interrotta durante purchasedonation; il QR non verrà reinviato automaticamente."}
                return self._finish(fingerprint, result, "AMBIGUOUS")
            donation_id = str(purchased["donation_id"])
            donation_url = str(purchased["donation_url"])
            self.store.update(fingerprint, "PURCHASED", donation_id=donation_id, donation_url=donation_url)
            phase = "PURCHASED"

        donation = self.graphql.get_donation(donation_id)
        if not _recipient_is_tiremm(donation, recipient):
            if not is_new and phase == "RECIPIENT_SUBMITTING" and age >= self.stale_after:
                result = {"ok": False, "status": "AMBIGUOUS_RECIPIENT", "qr_fingerprint": fingerprint,
                          "donation_id": donation_id, "donation_url": donation_url}
                return self._finish(fingerprint, result, "AMBIGUOUS")
            self.store.update(fingerprint, "RECIPIENT_SUBMITTING")
            try:
                self.graphql.change_recipient(donation_id, str(recipient["id"]))
            except Exception:
                try:
                    donation = self.graphql.get_donation(donation_id)
                except Exception:
                    donation = {}
                if not _recipient_is_tiremm(donation, recipient):
                    result = {"ok": False, "status": "AMBIGUOUS_RECIPIENT", "qr_fingerprint": fingerprint,
                              "donation_id": donation_id, "donation_url": donation_url}
                    return self._finish(fingerprint, result, "AMBIGUOUS")
            donation = self.graphql.get_donation(donation_id)
            if not _recipient_is_tiremm(donation, recipient):
                result = {"ok": False, "status": "RECIPIENT_POSTCONDITION_FAILED", "qr_fingerprint": fingerprint,
                          "donation_id": donation_id, "donation_url": donation_url}
                return self._finish(fingerprint, result, "AMBIGUOUS")
        self.store.update(fingerprint, "RECIPIENT_SET", donation_id=donation_id, donation_url=donation_url)

        campaign = donation.get("campaign") if isinstance(donation.get("campaign"), Mapping) else {}
        instant_available = bool(campaign.get("instantWinIntegration"))
        instant = {"status": "NOT_AVAILABLE", "amount": None}
        if instant_available:
            self.store.update(fingerprint, "INSTANT_WIN_SUBMITTING")
            try:
                instant = _instant_result(self.graphql.verify_instant_win(donation_id))
            except Exception:
                instant = {"status": "UNKNOWN", "amount": None}

        result = {"ok": True, "status": "DONATED_TO_TIREMM", "qr_fingerprint": fingerprint,
                  "donation_id": donation_id, "donation_url": donation_url,
                  "recipient": {"id": recipient["id"], "name": recipient["name"]},
                  "instant_win": instant, "already_processed": False}
        phase = "COMPLETE" if instant.get("status") != "UNKNOWN" else "COMPLETE_WIN_UNKNOWN"
        self.store.update(fingerprint, phase, result=result)
        if instant.get("status") == "WIN":
            try:
                result["telegram_request_id"] = _queue_win(donation_id, instant.get("amount"), self.telegram_outbox)
                self.store.update(fingerprint, phase, result=result)
            except OSError:
                result["telegram_request_id"] = ""
        return result
