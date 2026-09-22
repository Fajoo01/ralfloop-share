from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from ralfloop_agent.domains.domain_approval import DomainApprovalPolicy
from ralfloop_agent.domains.domain_approval_store import DomainApprovalStore
from ralfloop_agent.unified_assistant.browser_mcp_adapter import (
    BROWSER_INTERACT_ACTION,
    BrowserMCPApprovalProvider,
    UnifiedBrowserApprovalCoordinator,
    UnifiedBrowserApprovalExecutor,
    build_browser_interaction_scope,
    prepare_browser_interaction_payload,
)
from ralfloop_agent.unified_assistant.contracts import AssistantFeatureFlags, PolicyClass
from ralfloop_agent.unified_assistant.conversation import ConversationManager
from ralfloop_agent.unified_assistant.core import UnifiedAssistantCore
from ralfloop_agent.unified_assistant.planner import UnifiedPlanner
from ralfloop_agent.unified_assistant.registry import UnifiedRegistryFacade
from src.mcp_transport import MCPTool


class FakeSession:
    def __init__(self, calls):
        self.calls = calls

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def list_tools(self):
        return (
            MCPTool("browser_snapshot", "snapshot", {"type": "object"}),
            MCPTool("browser_click", "click", {"type": "object"}),
            MCPTool("browser_type", "type", {"type": "object"}),
            MCPTool("browser_file_upload", "upload", {"type": "object"}),
            MCPTool("browser_evaluate", "evaluate", {"type": "object"}),
            MCPTool("browser_run_code_unsafe", "unsafe", {"type": "object"}),
        )

    def call_tool(self, tool, arguments):
        self.calls.append((tool, dict(arguments)))
        if tool == "browser_snapshot":
            return {
                "content": [
                    {
                        "type": "text",
                        "text": '- button "Conferma" [ref=e7]\n- textbox "Nome" [ref=e8]',
                    }
                ]
            }
        return {"content": [{"type": "text", "text": f"ok:{tool}"}]}


def test_click_provider_uses_only_exact_allowlisted_tool():
    calls = []
    provider = BrowserMCPApprovalProvider(session_factory=lambda: FakeSession(calls))
    result = provider.apply(
        {"logical_action": "click", "target": "e7", "element": "Conferma"}
    )
    assert result["writes"] == 1
    assert calls == [
        ("browser_click", {"target": "e7", "element": "Conferma"})
    ]


def test_type_provider_never_implicitly_submits():
    calls = []
    provider = BrowserMCPApprovalProvider(session_factory=lambda: FakeSession(calls))
    result = provider.apply(
        {
            "logical_action": "type",
            "target": "e8",
            "element": "Nome",
            "text": "Fabio",
        }
    )
    assert result["writes"] == 1
    assert calls == [
        (
            "browser_type",
            {
                "target": "e8",
                "text": "Fabio",
                "submit": False,
                "element": "Nome",
            },
        )
    ]


def test_submit_is_bound_to_exact_click_target():
    calls = []
    provider = BrowserMCPApprovalProvider(session_factory=lambda: FakeSession(calls))
    result = provider.apply(
        {"logical_action": "submit", "target": "e7", "element": "Conferma"}
    )
    assert result["writes"] == 1
    assert calls == [
        ("browser_click", {"target": "e7", "element": "Conferma"})
    ]


def test_upload_requires_allowed_absolute_file_and_exact_sequence(tmp_path):
    upload = tmp_path / "documento.pdf"
    upload.write_bytes(b"pdf")
    calls = []
    stage = tmp_path / "stage"
    provider = BrowserMCPApprovalProvider(
        session_factory=lambda: FakeSession(calls),
        upload_roots=(tmp_path,),
        upload_staging_dir=stage,
    )
    result = provider.apply(
        {
            "logical_action": "upload",
            "target": "e7",
            "element": "Carica documento",
            "paths": [str(upload)],
        }
    )
    assert result["writes"] == 2
    assert calls[0] == (
        "browser_click", {"target": "e7", "element": "Carica documento"}
    )
    assert calls[1][0] == "browser_file_upload"
    staged_path = Path(calls[1][1]["paths"][0])
    assert staged_path.name == upload.name
    assert stage in staged_path.parents
    assert not staged_path.exists()
    assert list(stage.iterdir()) == []
    assert result["calls"][1]["arguments"] == {
        "paths": [str(upload.resolve())], "staged": True,
    }


def test_upload_payload_binds_file_content_hash(tmp_path):
    upload = tmp_path / "documento.pdf"
    upload.write_bytes(b"pdf")
    provider = BrowserMCPApprovalProvider(
        session_factory=lambda: FakeSession([]),
        upload_roots=(tmp_path,),
        upload_staging_dir=tmp_path / "stage",
    )
    payload = prepare_browser_interaction_payload(
        provider,
        {"logical_action": "upload", "target": "e7", "paths": [str(upload)]},
    )
    assert payload["upload_files"][0]["sha256"] == hashlib.sha256(b"pdf").hexdigest()
    upload.write_bytes(b"changed")
    with pytest.raises(ValueError, match="browser_upload_file_changed"):
        provider.apply(payload)


class FakeApprovalProvider:
    def __init__(self, before_text: str, after_text: str):
        self.before_text = before_text
        self.after_text = after_text
        self.snapshots = 0
        self.apply_calls = []

    def snapshot(self):
        self.snapshots += 1
        text = self.before_text if self.snapshots == 1 else self.after_text
        return {
            "text": text,
            "sha256": hashlib.sha256(text.encode()).hexdigest(),
        }

    def apply(self, scope):
        self.apply_calls.append(dict(scope))
        return {
            "ok": True,
            "logical_action": scope["logical_action"],
            "calls": [{"tool": "browser_click"}],
            "writes": 1,
        }


def _approved_pending(tmp_path, *, provider, pre_text):
    policy = DomainApprovalPolicy(
        enabled=True,
        allowed_user_ids={101},
        allowed_chat_ids={202},
        db_path=str(tmp_path / "approvals.sqlite"),
        audit_log=str(tmp_path / "audit.jsonl"),
    )
    store = DomainApprovalStore(policy=policy)
    manager = ConversationManager()
    payload = {
        "logical_action": "click",
        "target": "e7",
        "element": "Conferma",
        "pre_snapshot_sha256": hashlib.sha256(pre_text.encode()).hexdigest(),
        "pre_snapshot_excerpt": pre_text,
        "approval_summary": ["azione=click", "target=e7", "elemento=Conferma"],
    }
    pending = manager.stage(
        domain="browser",
        action=BROWSER_INTERACT_ACTION,
        policy=PolicyClass.CONFIRM_WRITE,
        payload=payload,
        displayed_text='Browser: cliccare "Conferma"?',
    )
    coordinator = UnifiedBrowserApprovalCoordinator(store, policy=policy)
    requested = coordinator.request(pending, requested_by="test")
    assert requested["status"] == "pending"
    attached = pending.model_copy(
        update={"approval_ref": requested["request_id"]}
    )
    approved = coordinator.approve(
        attached,
        telegram_user_id=101,
        telegram_chat_id=202,
        telegram_message_id=303,
    )
    assert approved["status"] == "approved"
    bound = attached.model_copy(
        update={"approved_digest": attached.payload_digest}
    )
    return store, bound


def test_executor_claims_once_and_requires_post_action_snapshot(tmp_path):
    before = '- button "Conferma" [ref=e7]'
    after = '- status "Salvato" [ref=e9]'
    provider = FakeApprovalProvider(before, after)
    store, pending = _approved_pending(
        tmp_path, provider=provider, pre_text=before
    )
    executor = UnifiedBrowserApprovalExecutor(store, provider)
    result = executor.execute(pending)
    assert result["status"] == "EXECUTED_VERIFIED"
    assert result["executed"] is True
    assert result["writes"] == 1
    assert result["post_snapshot_sha256"] == hashlib.sha256(after.encode()).hexdigest()
    assert len(provider.apply_calls) == 1
    assert store.get_request(pending.approval_ref)["status"] == "consumed"

    repeated = executor.execute(pending)
    assert repeated["status"] == "already_executed"
    assert len(provider.apply_calls) == 1


def test_executor_fails_closed_when_page_changes_after_approval(tmp_path):
    approved_snapshot = '- button "Conferma" [ref=e7]'
    changed_snapshot = '- button "Annulla" [ref=e7]'
    provider = FakeApprovalProvider(changed_snapshot, changed_snapshot)
    store, pending = _approved_pending(
        tmp_path, provider=provider, pre_text=approved_snapshot
    )
    executor = UnifiedBrowserApprovalExecutor(store, provider)
    result = executor.execute(pending)
    assert result["status"] == "DRAFT_CHANGED"
    assert result["writes"] == 0
    assert provider.apply_calls == []
    assert store.get_request(pending.approval_ref)["status"] == "stale"


def test_browser_scope_contains_no_arbitrary_tool_name(tmp_path):
    pre = '- button "Conferma" [ref=e7]'
    provider = FakeApprovalProvider(pre, pre)
    store, pending = _approved_pending(tmp_path, provider=provider, pre_text=pre)
    scope = build_browser_interaction_scope(pending)
    assert scope["logical_action"] == "click"
    assert scope["target"] == "e7"
    assert "tool" not in scope
    assert "browser_run_code_unsafe" not in repr(scope)


def test_planner_extracts_exact_browser_interaction_arguments():
    planner = UnifiedPlanner(UnifiedRegistryFacade())
    click = planner.plan("browser clicca e7")
    assert click.assignments[0].arguments == {"target": "e7", "logical_action": "click"}

    typed = planner.plan('browser scrivi su e8 testo="Fabio"')
    assert typed.assignments[0].arguments == {
        "target": "e8", "logical_action": "type", "text": "Fabio",
    }

    upload = planner.plan('browser carica e7 file="/tmp/documento.pdf"')
    assert upload.assignments[0].arguments == {
        "target": "e7", "logical_action": "upload", "paths": ["/tmp/documento.pdf"],
    }


class FakeBrowserExecutor:
    def __init__(self):
        self.calls = []

    def execute(self, pending):
        self.calls.append(pending)
        return {"status": "EXECUTED_VERIFIED", "executed": True, "writes": 1}


def test_core_stages_then_executes_browser_only_after_bound_approval():
    calls = []
    provider = BrowserMCPApprovalProvider(session_factory=lambda: FakeSession(calls))
    executor = FakeBrowserExecutor()
    manager = ConversationManager()
    core = UnifiedAssistantCore(
        planner=UnifiedPlanner(UnifiedRegistryFacade()),
        conversation=manager,
        flags=AssistantFeatureFlags(unified_assistant=True, browser_interact_live=True),
        browser_interaction_provider=provider,
        browser_approval_executor=executor,
    )

    staged = core.handle("browser clicca e7")
    assert staged.status == "draft_pending_approval"
    pending = manager.state.pending.browser
    assert pending is not None
    assert pending.payload["target"] == "e7"
    assert executor.calls == []
    assert core.handle("ok").status == "approval_required"

    manager.bind_approval(
        domain="browser", pending_id=pending.pending_id,
        payload_digest=pending.payload_digest, approval_ref="apr_browser",
    )
    executed = core.handle("ok")
    assert executed.status == "EXECUTED_VERIFIED"
    assert len(executor.calls) == 1
    assert manager.state.pending.browser is None


def test_request_scope_reuses_one_mcp_session_for_verified_action_cycle():
    calls = []
    created = []

    def factory():
        created.append(object())
        return FakeSession(calls)

    provider = BrowserMCPApprovalProvider(session_factory=factory)
    with provider.request_scope():
        provider.snapshot()
        provider.apply({"logical_action": "click", "target": "e7"})
        provider.snapshot()

    assert len(created) == 1
    assert [tool for tool, _args in calls] == [
        "browser_snapshot", "browser_click", "browser_snapshot",
    ]


def test_request_scope_is_lazy_when_browser_provider_is_unused():
    def factory():
        raise AssertionError("browser session opened without browser work")

    provider = BrowserMCPApprovalProvider(session_factory=factory)
    with provider.request_scope():
        pass


def test_resident_pool_reuses_client_and_invalidates_on_failure():
    from ralfloop_agent.unified_assistant.browser_mcp_adapter import _BrowserMCPSessionPool

    state = {"created": 0, "closed": 0}

    class PoolSession(FakeSession):
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            state["closed"] += 1

    def factory():
        state["created"] += 1
        return PoolSession([])

    pool = _BrowserMCPSessionPool(factory)
    with pool.transaction() as first:
        assert "browser_snapshot" in first[1]
    with pool.transaction() as second:
        assert first[0] is second[0]
    assert state == {"created": 1, "closed": 0}

    with pytest.raises(RuntimeError, match="boom"):
        with pool.transaction():
            raise RuntimeError("boom")
    assert state == {"created": 1, "closed": 1}
    with pool.transaction():
        pass
    assert state["created"] == 2
