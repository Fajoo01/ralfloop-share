from __future__ import annotations

from ralfloop_agent.unified_assistant.recipient import GenericRecipientResolver


class FakeGateway:
    account = "me@example.org"

    def __init__(self, messages):
        self.messages = messages
        self.calls = []

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return None

    def invoke(self, operation, **arguments):
        self.calls.append((operation, arguments))
        if operation == "search":
            return {"messages": self.messages}
        if operation == "read":
            row = next(item for item in self.messages if item["messageId"] == arguments["messageId"])
            return {"message": {
                "messageId": row["messageId"], "threadId": "abcdef0123456789",
                "from": row["sender"], "subject": row.get("subject", ""),
                "date": "2026-08-10", "body": "Evidence body",
            }}
        raise AssertionError(operation)


def test_exact_reply_source_verifies_sender_message_and_thread():
    class ExactGateway:
        account = "me@example.org"

        def __init__(self):
            self.calls = []

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return None

        def invoke(self, operation, **arguments):
            self.calls.append((operation, arguments))

            if operation == "read":
                assert arguments["messageId"] == "19fd1fbc9ff936d0"
                return {
                    "message": {
                        "messageId": "19fd1fbc9ff936d0",
                        "threadId": "",
                        "from": (
                            "Caterina Ghirelli "
                            "<caterina@circolomagnolia.it>"
                        ),
                        "subject": "Invito Festival",
                        "date": "Wed, 5 Aug 2026 14:52:32 +0200",
                        "body": "Invito.",
                    }
                }

            if operation == "getThread":
                assert arguments["threadId"] == "19fd1fbc9ff936d0"
                return {
                    "threadId": "19fd1fbc9ff936d0",
                    "messages": [{
                        "from": (
                            "Caterina Ghirelli "
                            "<caterina@circolomagnolia.it>"
                        ),
                        "subject": "Invito Festival",
                        "body": "Invito.",
                    }],
                }

            raise AssertionError(operation)

    gateway = ExactGateway()
    resolver = GenericRecipientResolver(
        lambda: gateway,
        account=gateway.account,
    )

    result = resolver.resolve_exact_reply(
        "caterina@circolomagnolia.it",
        "19fd1fbc9ff936d0",
    )

    assert result["status"] == "resolved"
    assert result["address"] == "caterina@circolomagnolia.it"
    assert result["source"] == "explicit_message_verified"
    assert (
        result["source_email"]["message_id"]
        == "19fd1fbc9ff936d0"
    )
    assert (
        result["source_email"]["thread_id"]
        == "19fd1fbc9ff936d0"
    )
    assert [call[0] for call in gateway.calls] == [
        "read",
        "getThread",
    ]


def test_exact_reply_source_sender_mismatch_fails_closed():
    class WrongSenderGateway:
        account = "me@example.org"

        def __init__(self):
            self.calls = []

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return None

        def invoke(self, operation, **arguments):
            self.calls.append((operation, arguments))

            if operation == "read":
                return {
                    "message": {
                        "messageId": "19fd1fbc9ff936d0",
                        "threadId": "19fd1fbc9ff936d0",
                        "from": "Altra Persona <other@example.org>",
                        "subject": "Invito",
                        "body": "Data.",
                    }
                }

            raise AssertionError(operation)

    gateway = WrongSenderGateway()
    resolver = GenericRecipientResolver(
        lambda: gateway,
        account=gateway.account,
    )

    result = resolver.resolve_exact_reply(
        "caterina@circolomagnolia.it",
        "19fd1fbc9ff936d0",
    )

    assert result["status"] == "mismatch"
    assert result["reason"] == "source_sender_mismatch"
    assert [call[0] for call in gateway.calls] == ["read"]


def test_explicit_address_wins_without_gmail_lookup():
    gateway = FakeGateway([])
    resolver = GenericRecipientResolver(lambda: gateway, account=gateway.account)

    result = resolver.resolve("Sonia <sonia@example.org>")

    assert result["address"] == "sonia@example.org"
    assert result["source"] == "explicit_instruction"
    assert gateway.calls == []


def test_unique_verified_gmail_participant_resolves_with_thread_evidence():
    gateway = FakeGateway([{
        "messageId": "0123456789abcdef", "sender": "Sonia Rossi <sonia@example.org>",
        "subject": "Documenti",
    }])
    resolver = GenericRecipientResolver(lambda: gateway, account=gateway.account)

    result = resolver.resolve("Sonia")

    assert result["status"] == "resolved"
    assert result["address"] == "sonia@example.org"
    assert result["source_email"]["thread_id"] == "abcdef0123456789"
    assert [call[0] for call in gateway.calls] == ["search", "read", "getThread"]


def test_ambiguous_gmail_name_requires_clarification_and_no_read():
    gateway = FakeGateway([
        {"messageId": "0123456789abcdef", "sender": "Marco Bianchi <m.b@example.org>"},
        {"messageId": "fedcba9876543210", "sender": "Marco Verdi <m.v@example.org>"},
    ])
    resolver = GenericRecipientResolver(lambda: gateway, account=gateway.account)

    result = resolver.resolve("Marco")

    assert result["status"] == "ambiguous"
    assert len(result["candidates"]) == 2
    assert all("address" not in item for item in result["candidates"])
    assert [call[0] for call in gateway.calls] == ["search"]


def test_unknown_name_never_invents_address():
    gateway = FakeGateway([])
    resolver = GenericRecipientResolver(lambda: gateway, account=gateway.account)

    assert resolver.resolve("Persona inesistente") is None


def test_organization_name_can_resolve_from_verified_sender_domain_fallback():
    class DomainGateway(FakeGateway):
        def invoke(self, operation, **arguments):
            if operation == "search" and str(arguments.get("query") or "").startswith("from:"):
                self.calls.append((operation, arguments))
                return {"messages": []}
            return super().invoke(operation, **arguments)

    gateway = DomainGateway([{
        "messageId": "0123456789abcdef",
        "sender": "Caterina Ghirelli <caterina@circolomagnolia.it>",
        "subject": "Invito festival",
    }])

    result = GenericRecipientResolver(
        lambda: gateway, account=gateway.account,
    ).resolve("Magnolia")

    assert result["status"] == "resolved"
    assert result["address"] == "caterina@circolomagnolia.it"
    assert [call[0] for call in gateway.calls[:3]] == ["search", "search", "read"]


def test_redacted_search_sender_is_hydrated_read_only_before_matching():
    class RedactedGateway(FakeGateway):
        def invoke(self, operation, **arguments):
            if operation == "search":
                self.calls.append((operation, arguments))
                return {"messages": [{
                    "messageId": "0123456789abcdef", "sender": "Caterina <caterina@c…>",
                    "subject": "Invito Circolo Magnolia",
                }]}
            if operation == "read":
                self.calls.append((operation, arguments))
                return {"message": {
                    "messageId": "0123456789abcdef", "threadId": "abcdef0123456789",
                    "from": "Caterina Ghirelli <caterina@circolomagnolia.it>",
                    "subject": "Invito Circolo Magnolia", "body": "Evidence",
                }}
            raise AssertionError(operation)

    gateway = RedactedGateway([])
    result = GenericRecipientResolver(lambda: gateway, account=gateway.account).resolve("Magnolia")

    assert result["status"] == "resolved"
    assert result["address"] == "caterina@circolomagnolia.it"
    assert result["source_email"]["message_id"] == "0123456789abcdef"
    assert [call[0] for call in gateway.calls] == ["search", "read", "read"]
