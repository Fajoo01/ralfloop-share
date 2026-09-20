from __future__ import annotations

"""Pure helpers for the MD Android Goodify protocol.

This module performs no network I/O.  It only parses captured/API fixture
responses and builds local request bodies matching the reverse-engineered APK.
"""

from dataclasses import dataclass
import json
from typing import Any, Mapping

GOODIFY_BASE = "https://catalogomdapp.dedagroupwiz.it/api/goodify/"
GET_DONATION_URL = GOODIFY_BASE + "getdonation"
PURCHASE_DONATION_URL = GOODIFY_BASE + "purchasedonation"

DONATION_FIELDS = (
    "codice_negozio", "codice_operatore", "data_transazione",
    "date_qrlocked_locked", "Goodify_associationName",
    "Goodify_date_donation", "Goodify_donatedAmount",
    "Goodify_donatedValuta", "Goodify_donation", "Goodify_donationId",
    "Goodify_UrldonationId", "id_cassa", "id_transazione",
    "importo_speso", "intLockedVerify",
)

@dataclass(frozen=True)
class ParsedGoodifyResponse:
    code: str
    message: str
    donations: tuple[dict[str, Any], ...]

    @property
    def first_donation(self) -> dict[str, Any] | None:
        return self.donations[0] if self.donations else None


def _as_object(raw: str | bytes | Mapping[str, Any]) -> Mapping[str, Any]:
    if isinstance(raw, Mapping):
        return raw
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8")
    value = json.loads(raw)
    if not isinstance(value, Mapping):
        raise ValueError("md_goodify_response_not_object")
    return value


def parse_goodify_response(raw: str | bytes | Mapping[str, Any]) -> ParsedGoodifyResponse:
    value = _as_object(raw)
    payload = value.get("payload")
    rows: list[dict[str, Any]] = []

    if isinstance(payload, list) and payload and isinstance(payload[0], Mapping):
        donation_list = payload[0].get("Donation")
        if isinstance(donation_list, list):
            for item in donation_list:
                if isinstance(item, Mapping):
                    rows.append({field: item.get(field) for field in DONATION_FIELDS if field in item})
    return ParsedGoodifyResponse(
        code=str(value.get("code") or ""),
        message=str(value.get("messaggio") or ""),
        donations=tuple(rows),
    )


def build_get_donation_body(access_token: str) -> dict[str, str]:
    token = str(access_token or "").strip()
    if not token:
        raise ValueError("md_goodify_missing_access_token")
    return {"token": token}


def build_purchase_donation_body(access_token: str, qr_code: str) -> dict[str, str]:
    token = str(access_token or "").strip()
    qr = str(qr_code or "").strip()
    if not token:
        raise ValueError("md_goodify_missing_access_token")
    if not qr or len(qr) > 4096:
        raise ValueError("md_goodify_invalid_qr_code")
    return {"token": token, "qr_code": qr}

def preview_purchase_donation_body(qr_code: str) -> dict[str, Any]:
    qr = str(qr_code or "").strip()
    if not qr or len(qr) > 4096:
        raise ValueError("md_goodify_invalid_qr_code")
    return {
        "token_source": "runtime_access_token",
        "qr_code": qr,
        "fields": ["token", "qr_code"],
        "submission_performed": False,
        "side_effects": 0,
    }


def contract_summary() -> dict[str, Any]:
    return {
        "transport": "POST JSON",
        "getdonation": {"url": GET_DONATION_URL, "body_fields": ["token"]},
        "purchasedonation": {
            "url": PURCHASE_DONATION_URL,
            "body_fields": ["token", "qr_code"],
            "implemented_as_network_call": False,
        },
        "response_shape": "payload[0].Donation[]",
        "donation_fields": list(DONATION_FIELDS),
    }
