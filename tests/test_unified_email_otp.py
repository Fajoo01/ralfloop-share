from types import SimpleNamespace

from ralfloop_agent.unified_assistant.email_otp import EmailOtpGate


def test_otp_binding_is_persistent_idempotent_and_consumed_once(tmp_path):
    calls = {"request": 0, "verify": 0, "consume": 0}

    def build_scope(**kwargs):
        return {
            "action": "send_email", "to": [kwargs["to"]], "cc": [], "bcc": [],
            "subject": kwargs["subject"], "attachments": [],
        }

    def create_request(**_kwargs):
        calls["request"] += 1
        return {"request_id": "mailotp_test", "status": "pending"}

    def verify_telegram_request(**_kwargs):
        calls["verify"] += 1
        return {"request_id": "mailotp_test", "status": "approved", "approved": True}

    def consume_request(**_kwargs):
        calls["consume"] += 1
        return {"request_id": "mailotp_test", "status": "consumed", "authorized": True}

    gate = EmailOtpGate(
        module_path=tmp_path / "tool.py", db_path=tmp_path / "otp.sqlite",
        audit_path=tmp_path / "audit.jsonl", key_path=tmp_path / "key",
        binding_db_path=tmp_path / "bindings.sqlite",
    )
    gate._loaded = SimpleNamespace(
        build_scope=build_scope, create_request=create_request,
        verify_telegram_request=verify_telegram_request, consume_request=consume_request,
    )
    approval = {
        "request_id": "apr_MAGNOLIA", "action": "reply_email",
        "scope": {"recipient": "caterina@example.invalid", "subject": "Magnolia"},
    }

    first = gate.request_for_approval(approval)
    second = gate.request_for_approval(approval)
    authorized = gate.authorize(approval)
    replay = gate.authorize(approval)

    assert first["requested"] is True
    assert second["requested"] is False
    assert calls["request"] == 1
    assert authorized == {"status": "email_otp_authorized", "authorized": True}
    assert replay["status"] == "email_otp_required"
    assert calls["verify"] == 1
    assert calls["consume"] == 1


def test_telegram_reply_uses_verify_telegram_without_token_or_file(tmp_path):
    seen = {}

    def build_scope(**kwargs):
        return {
            "action": "send_email", "to": [kwargs["to"]], "cc": [], "bcc": [],
            "subject": kwargs["subject"], "attachments": [],
        }

    def create_request(**_kwargs):
        return {"request_id": "mailotp_reply", "status": "pending"}

    def verify_telegram_request(**kwargs):
        update = kwargs["update_loader"]()[0]
        seen.update(update)
        return {"status": "approved", "approved": True}

    gate = EmailOtpGate(
        module_path=tmp_path / "tool.py", db_path=tmp_path / "otp.sqlite",
        audit_path=tmp_path / "audit.jsonl", key_path=tmp_path / "key",
        binding_db_path=tmp_path / "bindings.sqlite",
    )
    module = SimpleNamespace(
        build_scope=build_scope, create_request=create_request,
        verify_telegram_request=verify_telegram_request,
        _telegram_config=lambda: ("unused", []),
    )
    gate._loaded = module
    approval = {
        "request_id": "apr_REPLY", "action": "reply_email",
        "scope": {"recipient": "caterina@example.invalid", "subject": "Magnolia"},
    }
    gate.request_for_approval(approval)

    result = gate.submit_telegram_reply(
        approval, otp="123456", telegram_chat_id=22, telegram_message_id=91,
    )

    assert result["approved"] is True
    assert seen["update_id"] == 91
    assert seen["message"]["text"] == "123456"
    assert seen["message"]["chat"]["id"] == 22
