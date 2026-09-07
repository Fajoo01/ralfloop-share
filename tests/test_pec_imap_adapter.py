from email.message import EmailMessage

import pytest

from ralfloop_agent.unified_assistant.pec_imap_adapter import (
    PecImapAdapter,
    PecImapConfig,
    PecImapError,
)


def make_message(
    subject: str,
    body: str,
    *,
    attachment: bool = False,
):
    msg = EmailMessage()
    msg["From"] = "ufficio@example.invalid"
    msg["To"] = "tiremm@example.invalid"
    msg["Subject"] = subject
    msg["Date"] = "Mon, 7 Sep 2026 10:00:00 +0200"
    msg["X-Ricevuta"] = "completa"
    msg.set_content(body)

    if attachment:
        msg.add_attachment(
            b"%PDF-synthetic",
            maintype="application",
            subtype="pdf",
            filename="allegato.pdf",
        )

    return msg.as_bytes()


class FakeImap:
    def __init__(self, messages):
        self.messages = dict(messages)
        self.readonly = None
        self.fetch_queries = []
        self.logged_in = False

    def login(self, username, password):
        assert username == "pec@example.invalid"
        assert password == "secret"
        self.logged_in = True
        return "OK", [b"logged"]

    def select(self, mailbox, readonly=False):
        assert mailbox == "INBOX"
        self.readonly = readonly
        return "OK", [str(len(self.messages)).encode()]

    def uid(self, command, *args):
        command = command.upper()

        if command == "SEARCH":
            return (
                "OK",
                [
                    " ".join(
                        sorted(
                            self.messages,
                            key=int,
                        )
                    ).encode()
                ],
            )

        if command == "FETCH":
            uid = str(args[0])
            query = str(args[1])
            self.fetch_queries.append(query)

            if uid not in self.messages:
                return "NO", []

            return (
                "OK",
                [
                    (
                        (
                            f"{uid} (UID {uid} "
                            "FLAGS () BODY[] "
                            f"{{{len(self.messages[uid])}}})"
                        ).encode(),
                        self.messages[uid],
                    )
                ],
            )

        raise AssertionError(command)

    def close(self):
        return "OK", []

    def logout(self):
        return "BYE", []


def config():
    return PecImapConfig(
        host="imap.example.invalid",
        username="pec@example.invalid",
        password="secret",
    )


def factory_for(messages):
    instance = FakeImap(messages)

    def factory(*args, **kwargs):
        return instance

    return instance, factory


def test_imap_read_is_readonly_and_uses_body_peek():
    instance, factory = factory_for({
        "1": make_message(
            "RUNTS pratica n. 2603942",
            "Comunicazione pratica RUNTS 2603942",
        )
    })

    adapter = PecImapAdapter(
        config(),
        client_factory=factory,
    )

    rows = adapter.list_messages(limit=1)

    assert len(rows) == 1
    assert rows[0].runts_reference == "2603942"
    assert rows[0].certified is True
    assert rows[0].native_id.startswith("imap.")
    assert instance.readonly is True
    assert instance.fetch_queries == [
        "(FLAGS BODY.PEEK[])"
    ]


def test_exact_runts_reference_search_scans_complete_mailbox():
    messages = {
        str(i): make_message(
            f"Messaggio ordinario {i}",
            "Nessuna pratica",
        )
        for i in range(1, 130)
    }

    messages["2"] = make_message(
        "RUNTS pratica n. 26039420",
        "RUNTS 26039420",
    )
    messages["1"] = make_message(
        "RUNTS pratica n. 2603942",
        "RUNTS 2603942",
    )

    instance, factory = factory_for(messages)

    adapter = PecImapAdapter(
        config(),
        client_factory=factory,
    )

    rows = adapter.find_by_runts_reference(
        "2603942",
        limit=1,
    )

    assert len(rows) == 1
    assert rows[0].runts_reference == "2603942"
    assert "26039420" not in rows[0].subject
    assert len(instance.fetch_queries) == 129


def test_get_message_uses_stable_native_id():
    _, factory = factory_for({
        "7": make_message(
            "RUNTS pratica n. 2603942",
            "RUNTS 2603942",
        )
    })

    adapter = PecImapAdapter(
        config(),
        client_factory=factory,
    )

    first = adapter.list_messages(limit=1)[0]
    again = adapter.get_message(first.native_id)

    assert again.native_id == first.native_id
    assert again.content_hash == first.content_hash


def test_attachment_download_is_read_only():
    _, factory = factory_for({
        "3": make_message(
            "RUNTS pratica n. 2603942",
            "RUNTS 2603942",
            attachment=True,
        )
    })

    adapter = PecImapAdapter(
        config(),
        client_factory=factory,
    )

    row = adapter.list_messages(limit=1)[0]

    assert len(row.attachments) == 1
    attachment = row.attachments[0]

    assert attachment.filename == "allegato.pdf"
    assert attachment.content_hash
    assert adapter.download_attachment(
        row.native_id,
        attachment.attachment_id,
    ) == b"%PDF-synthetic"


def test_invalid_native_id_fails_closed():
    _, factory = factory_for({})

    adapter = PecImapAdapter(
        config(),
        client_factory=factory,
    )

    with pytest.raises(
        PecImapError,
        match="pec_imap_native_id_invalid",
    ):
        adapter.get_message("7")


def test_environment_prefers_password_file(
    tmp_path,
    monkeypatch,
):
    secret = tmp_path / "pec-password"
    secret.write_text("from-file\n")

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
        "wrong",
    )
    monkeypatch.setenv(
        "BOTTAZZI_PEC_IMAP_PASSWORD_FILE",
        str(secret),
    )

    cfg = PecImapConfig.from_environment()

    assert cfg.password == "from-file"


def test_runts_idpr_subject_is_extracted():
    _, factory = factory_for({
        "241": make_message(
            "POSTA CERTIFICATA: [RUNTS Ufficio Lombardia] "
            "[CF 97826900157] [IDPR 2603942] "
            "Ricevuto nuovo messaggio",
            "Notifica automatica RUNTS",
        )
    })

    adapter = PecImapAdapter(
        config(),
        client_factory=factory,
    )

    row = adapter.list_messages(limit=1)[0]

    assert row.runts_reference == "2603942"
