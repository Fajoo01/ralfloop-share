from __future__ import annotations

import os

import pytest

from ralfloop_agent.integration.confirmation_store import pending_actions
from src.confirmation import confirmation_manager, get_confirmation
from src.google_client import GoogleClient
from src.google_client_fake import GoogleClientFake
from src.mcp_client import MCPClient, NeedsConfirmationError


def _reset_confirmations() -> None:
    confirmation_manager.pending.clear()
    pending_actions.clear()


def test_create_draft_fake():
    client = GoogleClientFake(draft_only=True)

    result = client.create_draft("user@example.invalid", "Subject", "Body")

    assert result["status"] == "draft_created"
    assert result["draft_only"] is True
    assert result["id"] == "draft_0"
    assert client.drafts == [result]


def test_send_email_fake():
    client = GoogleClientFake(draft_only=False)

    result = client.send_email("user@example.invalid", "Subject", "Body")

    assert result["status"] == "sent"
    assert result["draft_only"] is False
    assert result["id"] == "sent_0"
    assert client.sent == [result]


def test_google_client_real_creates_draft():
    if os.getenv("RUN_GOOGLE_REAL_TESTS") != "1":
        pytest.skip("real Google draft test disabled")
    client = GoogleClient(
        client_secrets_path=os.environ.get("RALF_GOOGLE_CLIENT_SECRETS"),
        token_path=os.environ.get("RALF_GOOGLE_TOKEN"),
        draft_only=True,
    )

    result = client.create_draft("user@example.invalid", "Ralf test draft", "test body")

    assert result["status"] == "draft_created"
    assert result["draft_only"] is True


def test_google_client_send_requires_confirmation(monkeypatch):
    _reset_confirmations()
    monkeypatch.setenv("RALF_MCP_GOOGLE_ENABLED", "1")
    client = MCPClient(google_client=GoogleClientFake(draft_only=True))

    with pytest.raises(NeedsConfirmationError) as exc:
        client.send_email("user@example.invalid", "Subject", "Body")

    confirmation = get_confirmation(exc.value.confirmation_id)
    assert confirmation is not None
    assert confirmation.status == "pending"
    assert confirmation.details["draft_only"] is True
    assert pending_actions[exc.value.confirmation_id]["executed"] is False
