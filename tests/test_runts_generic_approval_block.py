import ast
import asyncio
import json
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from ralfloop_agent.domains.domain_approval import DomainApprovalPolicy
from ralfloop_agent.domains.domain_approval_store import DomainApprovalStore
from ralfloop_agent.domains.telegram_approval_api import (
    decision_headers,
    handle_decision_request,
)

from domain_approval_fixtures import approval_env


def _request(store):
    return store.create_request(
        action="runts_practice_reply",
        bando_id="runts.messaggistica",
        version="1",
        scope={"practice_id": "2603942", "pdf_sha256": "a" * 64},
        requested_by="test",
    )["request"]


def _generic_approve(policy, store, request, key, *, nonce, idempotency_key):
    payload = {
        "decision": "approve",
        "scope_digest_short": request["scope_digest_short"],
        "telegram_user_id": 111,
        "telegram_chat_id": 111,
        "telegram_message_id": 7,
        "idempotency_key": idempotency_key,
    }
    body = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()
    path = f"/domain-approvals/{request['request_id']}/decision"
    headers = decision_headers("POST", path, payload, key_file=key, nonce=nonce)
    return handle_decision_request(
        request_id=request["request_id"], body=body, headers=headers,
        path=path, policy=policy, store=store,
    )


@pytest.mark.parametrize("client_kind", ["rl:approve", "reply:approvo"])
def test_generic_clients_cannot_approve_runts(monkeypatch, approval_env, client_kind):
    key = approval_env / "hmac.key"
    key.write_text("secret", encoding="utf-8")
    monkeypatch.setenv("RALFLOOP_TELEGRAM_APPROVAL_HMAC_KEY_FILE", str(key))
    policy = DomainApprovalPolicy.from_env()
    store = DomainApprovalStore(policy=policy)
    request = _request(store)

    result = _generic_approve(
        policy, store, request, key,
        nonce=f"nonce-{client_kind}", idempotency_key=f"generic-{client_kind}",
    )

    assert result == {
        "status": "domain_specific_approval_required",
        "request_id": request["request_id"],
        "execution_allowed": False,
        "auto_execute": False,
        "writes": 0,
        "reason": "RUNTS approval requires: Approvo <practice_id>",
    }
    assert store.get_request(request["request_id"])["status"] == "pending"
    with store.connect() as connection:
        assert connection.execute("select count(*) from approval_executions").fetchone()[0] == 0
        assert connection.execute("select count(*) from approval_decisions").fetchone()[0] == 0


def test_runts_message_advertises_only_practice_bound_approval(approval_env):
    store = DomainApprovalStore()
    request = _request(store)
    message = request["telegram_message"]

    assert "Approvo 2603942" in message
    assert "rl:approve" not in message
    assert "con: approvo" not in message


def test_generic_approval_remains_available_for_other_domains(monkeypatch, approval_env):
    key = approval_env / "hmac.key"
    key.write_text("secret", encoding="utf-8")
    monkeypatch.setenv("RALFLOOP_TELEGRAM_APPROVAL_HMAC_KEY_FILE", str(key))
    policy = DomainApprovalPolicy.from_env()
    store = DomainApprovalStore(policy=policy)
    request = store.create_request(
        action="run_domain_canary", bando_id="test", version="1",
        scope={"canary_plan": {}}, requested_by="test",
    )["request"]

    result = _generic_approve(
        policy, store, request, key, nonce="other-domain",
        idempotency_key="other-domain",
    )

    assert result["status"] == "approved"
    assert store.get_request(request["request_id"])["status"] == "approved"


def test_meowgram_bare_reply_reaches_server_guard(monkeypatch, approval_env):
    source = Path("/srv/projects/Meowgram/src/meowgram/bot.py")
    if not source.is_file():
        pytest.skip("installed Meowgram source required")
    tree = ast.parse(source.read_text(encoding="utf-8"))
    method = next(
        node for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef)
        and node.name == "_parse_ralfloop_approval_short_reply"
    )
    method.returns = None
    for argument in (*method.args.args, *method.args.kwonlyargs):
        argument.annotation = None
    namespace = {"re": re}
    exec(compile(ast.Module(body=[method], type_ignores=[]), str(source), "exec"), namespace)

    class FakeBot:
        def _ralfloop_approval_reply_message_id(self, _message):
            return 99

        def _ralfloop_approval_reply_scope(self, _chat, _message):
            return {"request_id": request["request_id"],
                    "digest": request["scope_digest_short"]}

        async def _ralfloop_approval_reply_scope_from_message(self, _message):
            return None

    key = approval_env / "hmac.key"
    key.write_text("secret", encoding="utf-8")
    monkeypatch.setenv("RALFLOOP_TELEGRAM_APPROVAL_HMAC_KEY_FILE", str(key))
    policy = DomainApprovalPolicy.from_env()
    store = DomainApprovalStore(policy=policy)
    request = _request(store)
    parsed = asyncio.run(namespace[method.name](
        FakeBot(), SimpleNamespace(chat_id=111, sender_id=111),
        SimpleNamespace(), "approvo",
    ))
    assert parsed["command"] == "approve"

    result = _generic_approve(
        policy, store, request, key, nonce="meowgram-reply",
        idempotency_key="meowgram-reply",
    )
    assert result["status"] == "domain_specific_approval_required"
    assert store.get_request(request["request_id"])["status"] == "pending"
