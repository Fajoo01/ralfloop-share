from ralfloop_agent.unified_assistant.pec_imap_adapter import (
    PecImapError,
)
from ralfloop_agent.unified_assistant.pec_provider_factory import (
    PecFallbackReadProvider,
    imap_environment_present,
)


class Primary:
    def __init__(self, *, fail=False):
        self.fail = fail
        self.calls = []

    def list_messages(self, *, limit):
        self.calls.append(("list", limit))
        if self.fail:
            raise PecImapError("pec_imap_source_unavailable")
        return ("imap-list",)

    def find_by_runts_reference(self, reference, *, limit):
        self.calls.append(("find", reference, limit))
        if self.fail:
            raise PecImapError("pec_imap_source_unavailable")
        return ("imap-find",)

    def get_message(self, native_id):
        self.calls.append(("get", native_id))
        return ("imap-get", native_id)

    def download_attachment(self, message_id, attachment_id):
        self.calls.append(("attachment", message_id, attachment_id))
        return b"imap-attachment"


class Fallback:
    def __init__(self):
        self.calls = []
        self.closed = False

    def list_messages(self, *, limit):
        self.calls.append(("list", limit))
        return ("browser-list",)

    def find_by_runts_reference(self, reference, *, limit):
        self.calls.append(("find", reference, limit))
        return ("browser-find",)

    def get_message(self, native_id):
        self.calls.append(("get", native_id))
        return ("browser-get", native_id)

    def close(self):
        self.closed = True


def test_primary_imap_is_preferred():
    primary = Primary()
    fallback = Fallback()

    provider = PecFallbackReadProvider(
        primary,
        fallback,
    )

    assert provider.list_messages(limit=10) == ("imap-list",)
    assert provider.find_by_runts_reference(
        "2603942",
        limit=10,
    ) == ("imap-find",)

    assert fallback.calls == []


def test_browser_fallback_on_imap_failure():
    primary = Primary(fail=True)
    fallback = Fallback()

    provider = PecFallbackReadProvider(
        primary,
        fallback,
    )

    assert provider.list_messages(
        limit=10
    ) == ("browser-list",)

    assert provider.find_by_runts_reference(
        "2603942",
        limit=10,
    ) == ("browser-find",)

    assert fallback.calls == [
        ("list", 10),
        ("find", "2603942", 10),
    ]


def test_native_id_routes_to_correct_provider():
    primary = Primary()
    fallback = Fallback()

    provider = PecFallbackReadProvider(
        primary,
        fallback,
    )

    assert provider.get_message(
        "imap.0123456789ab.77"
    ) == (
        "imap-get",
        "imap.0123456789ab.77",
    )

    assert provider.get_message(
        "browser-id-7"
    ) == (
        "browser-get",
        "browser-id-7",
    )


def test_close_closes_browser_transport():
    primary = Primary()
    fallback = Fallback()

    provider = PecFallbackReadProvider(
        primary,
        fallback,
    )

    provider.close()

    assert fallback.closed is True


def test_imap_environment_detection(monkeypatch):
    monkeypatch.delenv(
        "BOTTAZZI_PEC_IMAP_HOST",
        raising=False,
    )
    monkeypatch.delenv(
        "BOTTAZZI_PEC_IMAP_USERNAME",
        raising=False,
    )
    monkeypatch.delenv(
        "BOTTAZZI_PEC_IMAP_PASSWORD",
        raising=False,
    )
    monkeypatch.delenv(
        "BOTTAZZI_PEC_IMAP_PASSWORD_FILE",
        raising=False,
    )

    assert imap_environment_present() is False

    monkeypatch.setenv(
        "BOTTAZZI_PEC_IMAP_HOST",
        "imap.example.invalid",
    )
    monkeypatch.setenv(
        "BOTTAZZI_PEC_IMAP_USERNAME",
        "pec@example.invalid",
    )
    monkeypatch.setenv(
        "BOTTAZZI_PEC_IMAP_PASSWORD",
        "secret",
    )

    assert imap_environment_present() is True
