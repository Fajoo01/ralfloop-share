from __future__ import annotations

from ralfloop_agent.unified_assistant import runtime
from ralfloop_agent.unified_assistant.core import EmailPipelineResult
from ralfloop_agent.unified_assistant.email_search import GoogleWorkspaceEmailSearch
from ralfloop_agent.unified_assistant.fastweb_portal import FastwebPortalResult
from ralfloop_agent.unified_assistant.runtime import is_unified_telegram_request, unified_route_probe
from ralfloop_agent.cli.session_store import SessionStore
from ralfloop_agent.unified_assistant.contracts import PolicyClass
from ralfloop_agent.unified_assistant.conversation import SessionConversationAdapter


def _pending_session(monkeypatch, tmp_path, *domains):
    root = tmp_path / "sessions"
    monkeypatch.setenv("RALFLOOP_UNIFIED_SESSION_DIR", str(root))
    store = SessionStore(root)
    session_id = "telegram-22-11"
    runtime._ensure_session(store, session_id)
    adapter = SessionConversationAdapter(store)
    conversation = adapter.load(session_id)
    for domain in domains:
        pending = conversation.stage(
            domain=domain,
            action=(
                "mailchimp_campaign_create" if domain == "mailchimp"
                else "runts_practice_reply" if domain == "runts"
                else "send_email"
            ),
            policy=PolicyClass.CONFIRM_WRITE,
            payload=(
                {"practice_id": "2603942", "content": "approved artifact"}
                if domain == "runts"
                else {"domain": domain, "content": "approved artifact"}
            ),
            displayed_text="Approved artifact",
        )
        conversation.attach_approval_request(
            domain=domain,
            pending_id=pending.pending_id,
            payload_digest=pending.payload_digest,
            approval_ref=("apr_ABCDEFGH" if domain == "mailchimp"
                          else "apr_RUNTS123" if domain == "runts"
                          else "apr_IJKLMNOP"),
            created_at=pending.created_at,
            expires_at=pending.expires_at,
        )
    adapter.save(session_id, conversation)
    return {
        "source": "telegram_natural",
        "telegram_user_id": 11,
        "telegram_chat_id": 22,
        "telegram_message_id": 1,
    }


def test_telegram_bridge_is_feature_flagged_and_legacy_first(monkeypatch):
    context = {
        "source": "telegram_natural", "telegram_user_id": 1,
        "telegram_chat_id": 1, "telegram_message_id": 1,
    }
    monkeypatch.setenv("RALFLOOP_UNIFIED_ASSISTANT", "0")
    assert not is_unified_telegram_request("Quanto fa in soggiorno?", context)

    monkeypatch.setenv("RALFLOOP_UNIFIED_ASSISTANT", "1")
    assert is_unified_telegram_request("Quanto fa in soggiorno?", context)
    assert not is_unified_telegram_request("/help", context)
    assert not is_unified_telegram_request("rl:approvals", context)
    assert not is_unified_telegram_request("apri cancello", context)
    assert not is_unified_telegram_request("Quanto fa in soggiorno?", {"source": "api"})
    assert not is_unified_telegram_request("Quanto spazio libero abbiamo?", context)


def test_bare_approval_routes_only_with_one_existing_mailchimp_pending(monkeypatch, tmp_path):
    monkeypatch.setenv("RALFLOOP_UNIFIED_ASSISTANT", "1")
    context = _pending_session(monkeypatch, tmp_path, "mailchimp")

    assert is_unified_telegram_request("approvo", context) is True


def test_bare_manda_routes_only_with_one_existing_email_pending(monkeypatch, tmp_path):
    monkeypatch.setenv("RALFLOOP_UNIFIED_ASSISTANT", "1")
    context = _pending_session(monkeypatch, tmp_path, "email")

    assert is_unified_telegram_request("manda", context) is True


def test_arci_appello_is_unified_grant_email_route(monkeypatch):
    monkeypatch.setenv("RALFLOOP_UNIFIED_ASSISTANT", "1")
    context = {
        "source": "ralf_terminal", "telegram_user_id": 1,
        "telegram_chat_id": 1, "telegram_message_id": 1,
    }

    route = unified_route_probe(
        "Rispondi all'appello di ARCI Milano per il bando: Tiremm Innanz è interessata",
        context,
    )

    assert route is not None
    assert route["task_mode"] == "external_action"
    assert route["skills_used"] == [
        "email.search", "bandi.read", "bandi.eligibility", "email.compose"
    ]
    assert route["mcp_used"] == ["google_workspace.gmail", "bandi.research.mcp"]
    assert route["requires_confirmation"] is True


def test_bare_approval_without_pending_keeps_legacy_routing(monkeypatch, tmp_path):
    monkeypatch.setenv("RALFLOOP_UNIFIED_ASSISTANT", "1")
    context = _pending_session(monkeypatch, tmp_path)

    assert is_unified_telegram_request("approvo", context) is False


def test_bare_approval_with_multiple_pending_fails_closed(monkeypatch, tmp_path):
    monkeypatch.setenv("RALFLOOP_UNIFIED_ASSISTANT", "1")
    context = _pending_session(monkeypatch, tmp_path, "mailchimp", "email")

    assert is_unified_telegram_request("approvo", context) is False


def test_explicit_runts_precedes_legacy_and_has_no_double_confirmation(monkeypatch, tmp_path):
    monkeypatch.setenv("RALFLOOP_UNIFIED_ASSISTANT", "1")
    context = _pending_session(monkeypatch, tmp_path, "runts")

    assert runtime._RUNTS_EXPLICIT_APPROVAL.fullmatch("Approvo 2603942")
    assert runtime._is_explicit_runts_approval("Approvo 2603942", context) is True
    assert is_unified_telegram_request("Approvo 2603942", context) is True
    route = unified_route_probe("Approvo 2603942", context)
    assert route["intent"] == "runts.practice.reply.approve"
    assert route["task_mode"] == "external_action"
    assert route["requires_confirmation"] is False
    for text in ("approvo", "ok", "sì", "si", "confermo"):
        assert runtime._RUNTS_EXPLICIT_APPROVAL.fullmatch(text) is None


def _expire_pending(context, domain):
    adapter = SessionConversationAdapter(runtime._session_store())
    conversation = adapter.load("telegram-22-11")
    item = getattr(conversation.state.pending, domain).model_copy(update={"expires_at": 0})
    pending = conversation.state.pending.model_copy(update={domain: item})
    conversation.state = conversation.state.model_copy(update={"pending": pending})
    adapter.save("telegram-22-11", conversation)


def test_expired_other_domain_does_not_ambiguous_active_runts(monkeypatch, tmp_path):
    monkeypatch.setenv("RALFLOOP_UNIFIED_ASSISTANT", "1")
    monkeypatch.setenv("RALFLOOP_ENABLE_TELEGRAM_APPROVAL_GATE", "1")
    monkeypatch.setenv("RALFLOOP_TELEGRAM_APPROVAL_DB", str(tmp_path / "approval.sqlite"))
    context = _pending_session(monkeypatch, tmp_path, "mailchimp", "runts")
    _expire_pending(context, "mailchimp")

    result = runtime._execute_explicit_runts_approval("Approvo 2603942", context)
    assert result["metadata"]["status"] == "approval_request_missing"
    assert result["metadata"]["writes"] == 0


def test_active_other_domain_keeps_runts_approval_ambiguous(monkeypatch, tmp_path):
    monkeypatch.setenv("RALFLOOP_UNIFIED_ASSISTANT", "1")
    context = _pending_session(monkeypatch, tmp_path, "mailchimp", "runts")

    result = runtime._execute_explicit_runts_approval("Approvo 2603942", context)
    assert result["metadata"]["status"] == "clarification_required"
    assert result["metadata"]["writes"] == 0


def test_expired_runts_is_not_approvable(monkeypatch, tmp_path):
    monkeypatch.setenv("RALFLOOP_UNIFIED_ASSISTANT", "1")
    context = _pending_session(monkeypatch, tmp_path, "runts")
    _expire_pending(context, "runts")

    assert runtime._is_explicit_runts_approval("Approvo 2603942", context) is False
    result = runtime._execute_explicit_runts_approval("Approvo 2603942", context)
    assert result["metadata"]["status"] == "clarification_required"
    assert result["metadata"]["writes"] == 0


def test_expired_other_pending_does_not_ambiguate_active_runts(monkeypatch, tmp_path):
    monkeypatch.setenv("RALFLOOP_UNIFIED_ASSISTANT", "1")
    context = _pending_session(monkeypatch, tmp_path, "mailchimp", "runts")
    adapter = SessionConversationAdapter(SessionStore(tmp_path / "sessions"))
    conversation = adapter.load("telegram-22-11")
    expired = conversation.state.pending.mailchimp.model_copy(update={"expires_at": 0})
    conversation.state = conversation.state.model_copy(update={
        "pending": conversation.state.pending.model_copy(update={"mailchimp": expired})
    })
    adapter.save("telegram-22-11", conversation)

    assert runtime._is_explicit_runts_approval("Approvo 2603942", context) is True
    assert is_unified_telegram_request("Approvo 2603942", context) is True


def test_active_other_pending_keeps_runts_approval_ambiguous(monkeypatch, tmp_path):
    monkeypatch.setenv("RALFLOOP_UNIFIED_ASSISTANT", "1")
    context = _pending_session(monkeypatch, tmp_path, "mailchimp", "runts")
    assert runtime._is_explicit_runts_approval("Approvo 2603942", context) is True
    result = runtime.run_unified_telegram("Approvo 2603942", context)
    assert result["metadata"]["status"] == "clarification_required"
    assert result["metadata"]["writes"] == 0


def test_fastweb_read_is_unified_tool_backed_route(monkeypatch):
    monkeypatch.setenv("RALFLOOP_UNIFIED_ASSISTANT", "1")
    context = {
        "source": "telegram_natural", "telegram_user_id": 1,
        "telegram_chat_id": 1, "telegram_message_id": 1,
    }
    text = "Controlla se Fastweb ha mai comunicato un aumento"

    route = unified_route_probe(text, context)

    assert route is not None
    assert route["task_mode"] == "tool_backed_read"
    assert route["interaction_class"] == "TOOL_BACKED_READ"
    assert route["skills_used"] == ["email.search"]
    assert route["mcp_used"] == ["google_workspace.gmail"]


def test_fastweb_portal_and_comparison_routes_are_tool_backed_read(monkeypatch):
    monkeypatch.setenv("RALFLOOP_UNIFIED_ASSISTANT", "1")
    context = {
        "source": "telegram_natural", "telegram_user_id": 1,
        "telegram_chat_id": 1, "telegram_message_id": 1,
    }

    portal = unified_route_probe("Quanto paghiamo Fastweb?", context)
    compare = unified_route_probe("Confronta le mail Fastweb con il canone attuale", context)

    assert portal is not None
    assert portal["skills_used"] == ["fastweb.portal.read"]
    assert portal["mcp_used"] == ["fastweb.portal.read_only"]
    assert compare is not None
    assert compare["skills_used"] == ["email.search", "fastweb.portal.read", "fastweb.compare"]
    assert compare["mcp_used"] == ["google_workspace.gmail", "fastweb.portal.read_only"]


def test_whatsapp_tiremm_route_is_read_only_and_scope_bound(monkeypatch):
    monkeypatch.setenv("RALFLOOP_UNIFIED_ASSISTANT", "1")
    context = {
        "source": "telegram_natural", "telegram_user_id": 1,
        "telegram_chat_id": 1, "telegram_message_id": 1,
    }

    route = unified_route_probe("Cerca su WhatsApp Tiremm la chat del partner", context)

    assert route is not None
    assert route["task_mode"] == "tool_backed_read"
    assert route["skills_used"] == ["whatsapp.read"]
    assert route["mcp_used"] == ["whatsapp.web.mcp"]
    assert route["write_policy"] == "no_write"


def test_whatsapp_compose_route_is_external_action_and_confirmation_bound(monkeypatch):
    monkeypatch.setenv("RALFLOOP_UNIFIED_ASSISTANT", "1")
    context = {
        "source": "telegram_natural", "telegram_user_id": 1,
        "telegram_chat_id": 1, "telegram_message_id": 1,
    }

    route = unified_route_probe(
        "Scrivi su WhatsApp a Marco che arriviamo alle 18", context,
    )

    assert route is not None
    assert route["task_mode"] == "external_action"
    assert route["skills_used"] == ["whatsapp.compose"]
    assert route["mcp_used"] == ["whatsapp.web.mcp"]
    assert route["requires_confirmation"] is True
    assert route["write_policy"] == "policy_gated"


def test_fastweb_comparison_executes_gmail_and_portal_as_separate_data(monkeypatch, tmp_path):
    class Gateway:
        account = "fabio@tiremminnanz.com"

        def invoke(self, operation, **arguments):
            if operation == "search":
                return {"messages": [{
                    "messageId": "m1", "sender": "Fastweb <info@example.invalid>",
                    "subject": "Variazione canone", "date": "2026-01-01",
                }]}
            return {"message": {
                "messageId": "m1", "threadId": "t1",
                "from": "Fastweb <info@example.invalid>",
                "subject": "Variazione canone", "date": "2026-01-01",
                "body": "Comunicazione di aumento del canone.",
            }}

    class Context:
        def __enter__(self):
            return Gateway()

        def __exit__(self, *_):
            return None

    class Portal:
        def read(self):
            return FastwebPortalResult(
                status="FOUND", session_authenticated=True,
                offer_name="Fastweb Casa Start", current_fee="36,80 €",
                provenance=("fastweb_portal:myfastweb#current_fee",),
                read_operations=("cdp.list", "cdp.accessibility_snapshot"),
                response="Canone corrente: 36,80 €.",
            )

    service = GoogleWorkspaceEmailSearch(lambda: Context())
    monkeypatch.setenv("RALFLOOP_UNIFIED_ASSISTANT", "1")
    monkeypatch.setenv("RALFLOOP_UNIFIED_SESSION_DIR", str(tmp_path / "sessions"))
    monkeypatch.setattr(runtime.GoogleWorkspaceEmailSearch, "from_environment", lambda: service)
    monkeypatch.setattr(runtime.FastwebPortalReadOnly, "from_environment", lambda: Portal())
    context = {
        "source": "telegram_natural", "telegram_user_id": 1,
        "telegram_chat_id": 1, "telegram_message_id": 1,
    }

    result = runtime.run_unified_telegram(
        "Confronta le mail Fastweb con il canone attuale", context
    )

    assert result["ok"] is True
    assert result["metadata"]["status"] == "multi_domain_completed"
    assert "fonti restano separate" in result["response"]
    assert result["approval_required"] is False
    assert result["pending_confirmation_id"] is None


def test_fastweb_runtime_returns_structured_read_artifact_without_pending(monkeypatch, tmp_path):
    class Gateway:
        account = "fabio@tiremminnanz.com"

        def invoke(self, operation, **arguments):
            if operation == "search":
                return {"messages": [{
                    "messageId": "m1", "sender": "Fastweb <info@example.invalid>",
                    "subject": "Aumento canone", "date": "2026-01-01",
                }]}
            return {"message": {
                "messageId": "m1", "threadId": "t1", "from": "Fastweb <info@example.invalid>",
                "subject": "Aumento canone", "date": "2026-01-01",
                "body": "Comunichiamo un aumento del canone.",
            }}

    class Context:
        def __enter__(self):
            return Gateway()

        def __exit__(self, *_):
            return None

    service = GoogleWorkspaceEmailSearch(lambda: Context())
    monkeypatch.setenv("RALFLOOP_UNIFIED_ASSISTANT", "1")
    monkeypatch.setenv("RALFLOOP_UNIFIED_SESSION_DIR", str(tmp_path / "sessions"))
    monkeypatch.setattr(runtime.GoogleWorkspaceEmailSearch, "from_environment", lambda: service)
    context = {
        "source": "telegram_natural", "telegram_user_id": 1,
        "telegram_chat_id": 1, "telegram_message_id": 1,
    }

    result = runtime.run_unified_telegram(
        "Controlla se Fastweb ha mai comunicato un aumento", context
    )

    assert result["ok"] is True
    assert result["capability"] == "email.search"
    assert result["tools_executed"] is True
    assert result["approval_required"] is False
    assert result["pending_confirmation_id"] is None
    assert result["artifacts"][0]["artifact_type"] == "email_search_result"
    assert result["artifacts"][0]["side_effects"] == 0


def test_runtime_unique_telegram_ok_approves_and_sends_exact_fake_draft(monkeypatch, tmp_path):
    class Pipeline:
        def compose(self, working):
            return EmailPipelineResult(
                body="Grazie, abbiamo ricevuto i documenti.",
                hard_guard="passed", risk="low", final_validator="passed",
            )

        def revise(self, working, current_body, instruction):
            raise AssertionError("unexpected revision")

    class Resolver:
        def resolve(self, label):
            return {"status": "resolved", "name": "Marco", "address": "marco@example.invalid"}

    class Executor:
        def __init__(self):
            self.calls = []

        def execute(self, pending):
            self.calls.append(pending)
            return {
                "status": "executed", "sent": True,
                "provider_message_id": "0123456789abcdef",
                "recipient": pending.payload["recipient"],
                "body": pending.payload["body"],
            }

    executor = Executor()
    monkeypatch.setenv("RALFLOOP_UNIFIED_ASSISTANT", "1")
    monkeypatch.setenv("RALFLOOP_EMAIL_ASSISTANT_LIVE", "1")
    monkeypatch.setenv("RALFLOOP_ENABLE_TELEGRAM_APPROVAL_GATE", "1")
    monkeypatch.setenv("RALFLOOP_TELEGRAM_APPROVAL_ALLOWED_USER_IDS", "11")
    monkeypatch.setenv("RALFLOOP_TELEGRAM_APPROVAL_ALLOWED_CHAT_IDS", "22")
    monkeypatch.setenv("RALFLOOP_TELEGRAM_APPROVAL_DB", str(tmp_path / "approvals.sqlite"))
    monkeypatch.setenv("RALFLOOP_UNIFIED_SESSION_DIR", str(tmp_path / "sessions"))
    monkeypatch.setattr(runtime, "GenericEmailPipeline", lambda: Pipeline())
    monkeypatch.setattr(
        runtime.GoogleWorkspaceRecipientResolver,
        "from_environment",
        classmethod(lambda cls: Resolver()),
    )
    monkeypatch.setattr(
        runtime.UnifiedGmailApprovalExecutor,
        "from_environment",
        classmethod(lambda cls, *, store: executor),
    )
    base_context = {
        "source": "telegram_natural", "telegram_user_id": 11,
        "telegram_chat_id": 22, "telegram_message_id": 1,
    }

    preview = runtime.run_unified_telegram(
        "Scrivi a Marco che abbiamo ricevuto i documenti", base_context
    )
    approved = runtime.run_unified_telegram(
        "ok", {**base_context, "telegram_message_id": 2}
    )

    assert preview["metadata"]["status"] == "draft_pending_approval"
    assert preview["approval_required"] is True
    assert executor.calls == [executor.calls[0]]
    assert executor.calls[0].payload["body"] == "Grazie, abbiamo ricevuto i documenti."
    assert approved["metadata"]["status"] == "executed"
    assert approved["metadata"]["result"]["provider_message_id"] == "0123456789abcdef"
