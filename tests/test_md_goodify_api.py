from __future__ import annotations

import json

import pytest

from ralfloop_agent.unified_assistant.md_goodify_api import (
    DONATION_FIELDS,
    build_get_donation_body,
    build_purchase_donation_body,
    contract_summary,
    parse_goodify_response,
    preview_purchase_donation_body,
)


def _fixture() -> dict:
    return {
        "code": "",
        "messaggio": "ok",
        "payload": [{"Donation": [{
            "Goodify_associationName": "Tiremm Innanz APS",
            "Goodify_UrldonationId": "https://example.test/donation/123",
            "Goodify_donatedAmount": "1.00",
            "unknown_future_field": "preserved-by-server-not-needed-here",
        }]}],
    }

def test_parse_goodify_response_matches_apk_shape():
    result = parse_goodify_response(_fixture())
    assert result.code == ""
    assert result.message == "ok"
    assert len(result.donations) == 1
    first = result.first_donation
    assert first is not None
    assert first["Goodify_associationName"] == "Tiremm Innanz APS"
    assert first["Goodify_UrldonationId"].endswith("/123")
    assert "unknown_future_field" not in first


def test_parse_goodify_response_handles_null_or_empty_payload():
    assert parse_goodify_response({"messaggio": "none", "payload": None}).donations == ()
    assert parse_goodify_response({"payload": []}).first_donation is None
    with pytest.raises(ValueError, match="not_object"):
        parse_goodify_response("[]")


def test_body_builders_match_reverse_engineered_contract():
    assert build_get_donation_body("access-123") == {"token": "access-123"}
    assert build_purchase_donation_body("access-123", "QR-ABC") == {
        "token": "access-123", "qr_code": "QR-ABC"
    }

def test_preview_redacts_token_and_never_submits():
    preview = preview_purchase_donation_body("QR-ABC")
    assert preview["token_source"] == "runtime_access_token"
    assert preview["qr_code"] == "QR-ABC"
    assert preview["submission_performed"] is False
    assert preview["side_effects"] == 0


def test_invalid_builder_inputs_are_rejected():
    with pytest.raises(ValueError, match="missing_access_token"):
        build_get_donation_body("")
    with pytest.raises(ValueError, match="missing_access_token"):
        build_purchase_donation_body("", "QR")
    with pytest.raises(ValueError, match="invalid_qr_code"):
        build_purchase_donation_body("token", "")


def test_contract_is_static_and_has_all_apk_fields():
    contract = contract_summary()
    assert contract["purchasedonation"]["implemented_as_network_call"] is False
    assert contract["response_shape"] == "payload[0].Donation[]"
    assert set(contract["donation_fields"]) == set(DONATION_FIELDS)
