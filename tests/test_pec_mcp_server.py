from __future__ import annotations

from dataclasses import dataclass

from scripts.ralf_pec_mcp_server import PecMCPServer, _response


@dataclass
class Attachment:
    attachment_id: str
    filename: str

    def model_dump(self, mode="json"):
        return {"attachment_id": self.attachment_id, "filename": self.filename}


@dataclass
class Message:
    native_id: str
    sender: str
    subject: str
    body: str
    attachments: tuple[Attachment, ...] = ()
    runts_reference: str | None = None

    def model_dump(self, mode="json"):
        return {
            "native_id": self.native_id,
            "sender": self.sender,
            "subject": self.subject,
            "body": self.body,
            "attachments": [a.model_dump() for a in self.attachments],
            "runts_reference": self.runts_reference,
        }


class Provider:
    def __init__(self):
        self.rows = (
            Message("imap.1", "difensore@example.test", "Richiesta documentazione TARI", "Inviare gli avvisi", (Attachment("a1", "richiesta.pdf"),)),
            Message("imap.2", "other@example.test", "Altro", "Nessuna pratica"),
        )

    def list_messages(self, *, limit: int):
        return self.rows[:limit]

    def get_message(self, native_id: str):
        return next(row for row in self.rows if row.native_id == native_id)

    def download_attachment(self, message_id: str, attachment_id: str):
        assert message_id == "imap.1"
        assert attachment_id == "a1"
        return b"test-pdf-bytes"


def test_list_and_search_are_read_only():
    server = PecMCPServer(Provider())
    listed = server.call("pec_discover_messages", {"limit": 2})["structuredContent"]
    assert listed["ok"] is True
    assert listed["writes"] == 0 and listed["sends"] == 0
    found = server.call("pec_search_messages", {"query": "tari", "limit": 10})["structuredContent"]
    assert [row["native_id"] for row in found["messages"]] == ["imap.1"]


def test_exact_read_and_attachment_metadata():
    server = PecMCPServer(Provider())
    message = server.call("pec_get_message", {"message_id": "imap.1"})["structuredContent"]
    assert message["message"]["subject"] == "Richiesta documentazione TARI"
    attachments = server.call("pec_list_attachments", {"message_id": "imap.1"})["structuredContent"]
    assert attachments["attachments"] == [{"attachment_id": "a1", "filename": "richiesta.pdf"}]
    attachment = server.call("pec_get_attachment", {"message_id": "imap.1", "attachment_id": "a1"})["structuredContent"]
    assert attachment["attachment"]["size"] == len(b"test-pdf-bytes")
    assert attachment["attachment"]["data_base64"] == "dGVzdC1wZGYtYnl0ZXM="


def test_invalid_calls_fail_closed():
    server = PecMCPServer(Provider())
    assert server.call("pec_search_messages", {"query": ""})["isError"] is True
    assert server.call("pec_discover_messages", {"limit": 999})["isError"] is True
    assert server.call("unknown", {})["isError"] is True


def test_mcp_initialize_and_inventory():
    server = PecMCPServer(Provider())
    init = _response({"jsonrpc": "2.0", "id": 1, "method": "initialize"}, server)
    assert init["result"]["serverInfo"]["name"] == "ralf-pec-read"
    tools = _response({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, server)
    assert {row["name"] for row in tools["result"]["tools"]} == {
        "pec_discover_messages", "pec_get_message", "pec_list_attachments", "pec_get_attachment", "pec_search_messages"
    }


def test_systemd_broker_uses_release_and_shared_peer_auth_group():
    from pathlib import Path

    unit = Path("deploy/systemd/ralf-pec-mcp-broker.service").read_text(encoding="utf-8")
    assert "WorkingDirectory=/home/sibilla-cumana/ralfloop-production/current" in unit
    assert "Group=ralf-mcp" in unit
    assert "UMask=0007" in unit
    assert "RuntimeDirectoryMode=0770" in unit
    assert "/home/sibilla-cumana/ralfloop-production/current/scripts/ralf_pec_mcp_broker.py" in unit
    assert "--allow-group ralf-mcp" in unit
    assert "/home/sibilla-cumana/ralfloop-production/current/scripts/ralf_pec_mcp_server.py" in unit
