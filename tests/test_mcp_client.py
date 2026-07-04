import pytest

from src.confirmation import get_confirmation
from src.mcp_client import MCPClient, NeedsConfirmationError


def test_send_email_requires_confirmation():
    client = MCPClient()

    with pytest.raises(NeedsConfirmationError) as exc:
        client.send_email("user@example.invalid", "Subject", "Body")

    confirmation = get_confirmation(exc.value.confirmation_id)
    assert confirmation is not None
    assert confirmation.status == "pending"
    assert confirmation.action_type == "send_email"


def test_browser_inspect_no_confirmation():
    client = MCPClient()

    result = client.browser_inspect("https://example.invalid")

    assert "https://example.invalid" in result
