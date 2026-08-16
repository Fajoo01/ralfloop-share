from __future__ import annotations

import pytest

from ralfloop_agent.unified_assistant import browser_read_only
from ralfloop_agent.unified_assistant.browser_read_only import (
    BrowserPage,
    BrowserReadOnlyError,
    CdpReadOnlySnapshotClient,
)


def test_cdp_facade_denies_generic_method_and_expression_before_network():
    client = CdpReadOnlySnapshotClient("http://127.0.0.1:9236")
    page = BrowserPage("p", "x", "https://web.whatsapp.com/", "ws://127.0.0.1/none")

    with pytest.raises(BrowserReadOnlyError, match="cdp_method_denied"):
        client._call(page, "Page.navigate", {"url": "https://example.invalid"})
    with pytest.raises(BrowserReadOnlyError, match="cdp_expression_denied"):
        client._call(page, "Runtime.evaluate", {"expression": "document.body.click()"})


def test_whatsapp_media_script_is_fixed_bounded_read_only():
    script = browser_read_only._VISIBLE_MEDIA_SCRIPT

    assert "#main" in script
    assert "slice(0, 6)" in script
    assert "8388608" in script
    assert "method: 'GET'" in script
    assert ".click(" not in script
    assert ".submit(" not in script
    assert "blob:" in script and "data:" in script
