from __future__ import annotations

import json
from pathlib import Path

import pytest

from ralfloop_agent.unified_assistant.md_goodify import (
    classify_goodify_outcome,
    decode_qr_image,
    parse_md_goodify_qr,
)
from scripts.ralf_md_goodify_mcp_server import MdGoodifyMCPServer, TOOLS


def test_qr_parser_accepts_only_md_goodify_https_hosts():
    qr = parse_md_goodify_qr("Apri https://me.goodify.com/donate?code=ABC123")
    assert qr.host == "me.goodify.com"
    assert qr.token_hint == "ABC123"
    assert len(qr.fingerprint) == 64

    with pytest.raises(ValueError, match="not_allowlisted"):
        parse_md_goodify_qr("https://evilgoodify.com/donate?code=ABC")
    with pytest.raises(ValueError, match="not_allowlisted"):
        parse_md_goodify_qr("http://goodify.com/donate?code=ABC")


def test_qr_image_is_confined_to_spool(tmp_path: Path):
    root = tmp_path / "spool"
    root.mkdir()
    outside = tmp_path / "receipt.png"
    outside.write_bytes(b"not-an-image")
    with pytest.raises(ValueError, match="outside_spool"):
        decode_qr_image(str(outside), qr_root=root)


def test_outcome_classifier_prefers_explicit_loss_over_generic_prize_words():
    loss = classify_goodify_outcome("Concorso a premi", "Questa volta non hai vinto. Ritenta alla prossima occasione.")
    assert loss["status"] == "LOSS"

    win = classify_goodify_outcome("Complimenti", "Complimenti, hai vinto un voucher da 25 euro.")
    assert win["status"] == "WIN"
    assert "25" in win["amounts_eur"]

    unknown = classify_goodify_outcome("Tenta la Fortuna", "Hai una nuova possibilità disponibile.")
    assert unknown["status"] == "UNKNOWN"


def test_mcp_surface_has_no_gambling_or_generic_browser_tool():
    names = set(TOOLS)
    assert "md_goodify_parse_qr" in names
    assert "md_goodify_poll_mail" in names
    assert not any("gratta" in name or "fortuna" in name or "play" in name for name in names)
    serialized = json.dumps(TOOLS).casefold()
    for forbidden in ("selector", "xpath", "javascript", "cdp_method"):
        assert forbidden not in serialized


def test_prepare_donation_is_side_effect_free():
    server = MdGoodifyMCPServer()
    response = server.call("md_goodify_prepare_donation", {
        "payload": "https://www.goodify.com/path?code=ABC123",
        "nonprofit": "Tiremm Innanz APS",
    })
    result = response["structuredContent"]
    assert result["ok"] is True
    assert result["nonprofit"] == "Tiremm Innanz APS"
    assert result["submission_performed"] is False
    assert result["side_effects"] == 0


def test_mail_worker_forwards_allowlisted_win_and_queues_telegram(monkeypatch, tmp_path: Path):
    import ralfloop_agent.unified_assistant.md_goodify as mod
    import src.google_workspace as google_workspace

    calls = []

    class FakeSession:
        def __init__(self, *args, **kwargs): pass
        def initialize(self): return {}
        def list_tools(self): return ()
        def call_tool(self, name, arguments):
            calls.append((name, dict(arguments)))
            return {"structuredContent": {"ok": True, "messageId": "sent123"}}
        def close(self): pass

    class FakeGateway:
        def __init__(self, session, *, account): self.session, self.account = session, account
        def discover(self): return ("manage_email",)
        def invoke(self, operation, **arguments):
            if operation == "search":
                return {"messages": [{"messageId": "abcdef0123456789"}]}
            assert operation == "read"
            return {"message": {"messageId": "abcdef0123456789", "from": "Goodify <noreply@goodify.com>",
                "subject": "Complimenti", "date": "now", "body": "Complimenti, hai vinto un voucher da 10 euro."}}

    monkeypatch.setattr(mod, "UnixMCPTransport", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(mod, "MCPClientSession", FakeSession)
    monkeypatch.setattr(google_workspace, "GoogleWorkspaceGateway", FakeGateway)

    state = tmp_path / "state.json"
    outbox = tmp_path / "outbox.jsonl"
    result = mod.process_goodify_mailbox(
        account="source@example.org",
        forward_to="fabio@tiremminnanz.com",
        state_path=state,
        telegram_outbox=outbox,
        gmail_socket="/fake.sock",
    )

    assert result["wins"] == 1
    assert result["events"][0]["forwarded"] is True
    assert calls[0][0] == "manage_email"
    assert calls[0][1]["operation"] == "forward"
    assert calls[0][1]["to"] == "fabio@tiremminnanz.com"
    row = json.loads(outbox.read_text(encoding="utf-8").strip())
    assert row["kind"] == "md_goodify_win_notification"
    assert "vincita rilevata" in row["message"]

    again = mod.process_goodify_mailbox(
        account="source@example.org",
        forward_to="fabio@tiremminnanz.com",
        state_path=state,
        telegram_outbox=outbox,
        gmail_socket="/fake.sock",
    )
    assert again["processed_now"] == 0
