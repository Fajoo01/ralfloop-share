import json
import time

from ralfloop_agent.domains.domain_approval import DomainApprovalPolicy
from ralfloop_agent.domains.telegram_approval_api import decision_headers, handle_decision_request

from domain_approval_fixtures import approval_env


def test_hmac_valid_and_replay(monkeypatch, approval_env):
    key = approval_env / "hmac.key"
    key.write_text("secret", encoding="utf-8")
    monkeypatch.setenv("RALFLOOP_TELEGRAM_APPROVAL_HMAC_KEY_FILE", str(key))
    policy = DomainApprovalPolicy.from_env()
    payload = {"decision": "approve", "scope_digest_short": "ABCD-1234", "telegram_user_id": 111, "telegram_chat_id": 111, "telegram_message_id": 1, "idempotency_key": "k"}
    body = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()
    path = "/domain-approvals/apr_missing/decision"
    headers = decision_headers("POST", path, payload, key_file=key, nonce="n1")
    assert handle_decision_request(request_id="apr_missing", body=body, headers=headers, path=path, policy=policy)["status"] == "not_found"
    assert handle_decision_request(request_id="apr_missing", body=body, headers=headers, path=path, policy=policy)["status"] == "replay_detected"


def test_hmac_invalid_body_and_expired(monkeypatch, approval_env):
    key = approval_env / "hmac.key"
    key.write_text("secret", encoding="utf-8")
    monkeypatch.setenv("RALFLOOP_TELEGRAM_APPROVAL_HMAC_KEY_FILE", str(key))
    policy = DomainApprovalPolicy.from_env()
    payload = {"decision": "approve"}
    path = "/domain-approvals/apr_x/decision"
    headers = decision_headers("POST", path, payload, key_file=key, nonce="n2", timestamp=int(time.time()) - 999)
    assert handle_decision_request(request_id="apr_x", body=json.dumps(payload, sort_keys=True).encode(), headers=headers, path=path, policy=policy)["status"] == "signature_expired"
    headers = decision_headers("POST", path, payload, key_file=key, nonce="n3")
    assert handle_decision_request(request_id="apr_x", body=b'{"decision":"reject"}', headers=headers, path=path, policy=policy)["status"] == "signature_invalid"
