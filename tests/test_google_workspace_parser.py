from __future__ import annotations

import pytest

from src.google_workspace import _email_write_confirmation, _normalize_manage_email_result
from src.mcp_transport import MCPProtocolError


MID = "19fd1fbc9ff936d0"
TID = "19fd1fbc9ff936d1"


def mcp_text(text: str) -> dict:
    return {"content": [{"type": "text", "text": text}]}


SEARCH_MARKDOWN = """## Messages (2)

19fd1fbc9ff936d0 | Caterina <caterina@circolom… | Festival | bambini — "speciale" | Aug 5
19fd1fbc9ff936d2 | ARCI Milano <circoli@arci.it> | Convocazione territoriale | Jul 1

---
**Next steps:**
- Read a specific email
"""

READ_MARKDOWN = """## Invito | partecipazione — settembre

**From:** Caterina Ghirelli <caterina@circolomagnolia.it>
**To:** Tiremm Innanz <fabio@tiremminnanz.com>
**Date:** Wed, 5 Aug 2026 14:52:32 +0200
**Labels:** IMPORTANT, INBOX

Ciao!

Message ID: deadbeefdeadbeef
Questo è corpo, non metadato | e resta multilinea.

---
**Next steps:**
- Reply using the observed ID
"""

THREADS_MARKDOWN = f"""## Threads (1)

{TID} | Caterina per ARCI Magnolia, festival settembre

---
**Session context** (account):
- context
"""

THREAD_MARKDOWN = """## Thread (2 messages)

**Caterina <caterina@circolomagnolia.it>** — Wed, 5 Aug 2026 14:52:32 +0200
Subject: Invito | partecipazione
Primo corpo.
Message ID: badbadbadbadbadb

**Fabio <fabio@tiremminnanz.com>** — Thu, 6 Aug 2026 09:00:00 +0200
Subject: Re: Invito | partecipazione
Secondo corpo
su più righe.

---
**Session context** (account):
- context
"""


def test_search_real_markdown_subject_pipe_and_raw_audit():
    out = _normalize_manage_email_result("search", mcp_text(SEARCH_MARKDOWN))
    assert out["messages"][0] == {
        "messageId": MID,
        "sender": "Caterina <caterina@circolom…",
        "subject": 'Festival | bambini — "speciale"',
        "date": "Aug 5",
    }
    assert out["_raw_text"] == SEARCH_MARKDOWN


def test_read_real_markdown_multiline_body_never_promotes_body_id():
    out = _normalize_manage_email_result("read", mcp_text(READ_MARKDOWN), requested_message_id=MID)
    msg = out["message"]
    assert msg["messageId"] == MID and msg["threadId"] == ""
    assert msg["subject"] == "Invito | partecipazione — settembre"
    assert msg["from"].startswith("Caterina Ghirelli")
    assert msg["to"] == "Tiremm Innanz <fabio@tiremminnanz.com>"
    assert msg["labels"] == "IMPORTANT, INBOX"
    assert "deadbeefdeadbeef" in msg["body"]


def test_threads_and_get_thread_real_markdown():
    threads = _normalize_manage_email_result("threads", mcp_text(THREADS_MARKDOWN))
    assert threads["threads"] == [{"threadId": TID, "snippet": "Caterina per ARCI Magnolia, festival settembre"}]
    thread = _normalize_manage_email_result("getThread", mcp_text(THREAD_MARKDOWN), requested_thread_id=TID)
    assert thread["threadId"] == TID
    assert len(thread["messages"]) == 2
    assert thread["messages"][1]["body"] == "Secondo corpo\nsu più righe."
    assert "messageId" not in thread["messages"][0]


def test_structured_content_remains_supported_and_raw_is_preserved():
    out = _normalize_manage_email_result("search", {
        "structuredContent": {"messages": [{"messageId": MID}]},
        "content": [{"type": "text", "text": "audit"}],
    })
    assert out == {"messages": [{"messageId": MID}], "_raw_text": "audit"}


def test_real_google_workspace_mcp_reply_confirmation_markdown():
    result = mcp_text(
        "Reply sent.\n\n**Message ID:** 19fd1fbc9ff936d9\n\n---\n**Session context**"
    )

    assert _email_write_confirmation(result) == {
        "message_id": "19fd1fbc9ff936d9", "thread_id": "",
    }


@pytest.mark.parametrize("operation,result,kwargs,error", [
    ("search", mcp_text("not markdown"), {}, "malformed_search"),
    ("search", mcp_text("## Messages (1)\n\nmissing | Sender | Subject | Aug 5"), {}, "invalid_message_id"),
    ("read", mcp_text(READ_MARKDOWN), {}, "missing_message_id"),
    ("getThread", mcp_text(THREAD_MARKDOWN), {}, "missing_thread_id"),
    ("getThread", mcp_text("## Thread (1 messages)\n\nbody Message ID: deadbeefdeadbeef"), {"requested_thread_id": TID}, "malformed_thread_message"),
])
def test_malformed_or_missing_identifiers_fail_closed(operation, result, kwargs, error):
    with pytest.raises(MCPProtocolError, match=error):
        _normalize_manage_email_result(operation, result, **kwargs)
