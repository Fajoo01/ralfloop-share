from __future__ import annotations

import json
import time
from pathlib import Path

from ralfloop_agent.domains.domain_approval import (
    DomainApprovalDecision,
    DomainApprovalPolicy,
)
from ralfloop_agent.domains.domain_approval_store import DomainApprovalStore
from ralfloop_agent.integration.eyf_browser_executor import (
    APPROVAL_ACTION,
    EyfBrowserApprovalService,
    FINAL_SUBMIT_TARGET,
    _batch_digest_from_scope,
)


URL = "https://support4youth.coe.int/organization/profile"


class FakeBrowser:
    def __init__(
        self,
        *,
        url=URL,
        page="page-v1",
        fail_kind=None,
    ):
        self.url = url
        self.page = page
        self.fail_kind = fail_kind
        self.calls = []

    def snapshot(self):
        return {
            "url": self.url,
            "stable": {
                "page": self.page,
                "form": "organisation-profile",
            },
        }

    def _effect(self, kind, *args):
        self.calls.append((kind, *args))
        if self.fail_kind == kind:
            raise OSError("uncertain")
        return {"ok": True}

    def fill(self, target, value):
        return self._effect("fill", target, value)

    def upload(self, target, path):
        return self._effect("upload", target, path)

    def click(self, target):
        return self._effect("click", target)

    def submit(self, target):
        result = self._effect("submit", target)
        self.page = "page-after-submit"
        return result

    def verify_postconditions(self, operations, before, after):
        return {"ok": True, "operation_count": len(operations)}


class NoVerifierBrowser(FakeBrowser):
    verify_postconditions = None


def policy(tmp_path):
    return DomainApprovalPolicy(
        enabled=True,
        ttl_sec=300,
        max_pending=20,
        allowed_user_ids={11},
        allowed_chat_ids={22},
        require_private_chat=True,
        db_path=str(tmp_path / "approval.sqlite"),
        audit_log=str(tmp_path / "audit.jsonl"),
    )


def service(tmp_path, browser=None, *, allowed=()):
    p = policy(tmp_path)
    store = DomainApprovalStore(policy=p)
    return (
        EyfBrowserApprovalService(
            store,
            policy=p,
            browser=browser or FakeBrowser(),
            allowed_uploads=allowed,
            approval_outbox=tmp_path / "approval-outbox.jsonl",
        ),
        store,
    )


def approve(store, created, *, decision="approve", idem="eyf-test"):
    req = created["request"]
    return store.decide(
        DomainApprovalDecision(
            request_id=req["request_id"],
            decision=decision,
            telegram_user_id=11,
            telegram_chat_id=22,
            telegram_message_id=33,
            idempotency_key=idem,
        ),
        scope_digest_short=req["scope_digest_short"],
    )


def operations(file_path=None):
    out = [
        {
            "kind": "fill",
            "target": "organisation.name",
            "value": "Tiremm Innanz APS",
        },
    ]
    if file_path is not None:
        out.append(
            {
                "kind": "upload",
                "target": "proof.registration",
                "path": str(file_path),
            }
        )
    out.append({"kind": "click", "target": "save"})
    return out


def test_preview_is_read_only_and_hash_bound(tmp_path):
    browser = FakeBrowser()
    svc, _ = service(tmp_path, browser)

    result = svc.preview(operations())

    assert result["status"] == "preview"
    assert result["approval_required"] is True
    assert result["executed"] is False
    assert len(result["page_sha256"]) == 64
    assert len(result["batch_sha256"]) == 64
    assert browser.calls == []


def test_pending_rejected_and_expired_never_touch_browser(tmp_path):
    browser = FakeBrowser()
    svc, store = service(tmp_path, browser)

    created = svc.request(operations(), requested_by="test")
    req = created["request"]

    assert svc.execute(req["request_id"])["status"] == "pending"
    assert browser.calls == []

    rejected = svc.request(operations(), requested_by="test")
    approve(store, rejected, decision="reject", idem="reject")
    assert svc.execute(rejected["request"]["request_id"])["status"] == "rejected"
    assert browser.calls == []

    expired = svc.request(operations(), requested_by="test")
    with store.connect() as conn:
        conn.execute(
            "update approval_requests set expires_at = ? where request_id = ?",
            (
                int(time.time()) - 10,
                expired["request"]["request_id"],
            ),
        )
    assert svc.execute(expired["request"]["request_id"])["status"] == "expired"
    assert browser.calls == []


def test_request_without_telegram_outbox_cancels_fail_closed(tmp_path, monkeypatch):
    monkeypatch.delenv("RALFLOOP_TELEGRAM_APPROVAL_OUTBOX", raising=False)
    p = policy(tmp_path)
    store = DomainApprovalStore(policy=p)
    svc = EyfBrowserApprovalService(store, policy=p, browser=FakeBrowser())

    result = svc.request(operations(), requested_by="test")

    assert result["status"] == "approval_notification_unconfigured"
    assert result["notification_queued"] is False
    assert store.get_request(result["request_id"])["status"] == "cancelled"

def test_wrong_host_is_fail_closed(tmp_path):
    svc, store = service(
        tmp_path,
        FakeBrowser(url="https://example.invalid/profile"),
    )

    result = svc.request(operations(), requested_by="test")

    assert result["status"] == "host_forbidden"
    assert store.list_pending() == []


def test_nonallowlisted_upload_is_rejected(tmp_path):
    document = tmp_path / "registration.pdf"
    document.write_bytes(b"registration")

    svc, store = service(tmp_path)

    result = svc.request(
        operations(document),
        requested_by="test",
    )

    assert result["status"] == "upload_not_allowlisted"
    assert store.list_pending() == []


def test_page_mutation_marks_stale_before_claim(tmp_path):
    browser = FakeBrowser()
    svc, store = service(tmp_path, browser)

    created = svc.request(operations(), requested_by="test")
    approve(store, created)

    browser.page = "changed-before-execution"

    result = svc.execute(created["request"]["request_id"])

    assert result["status"] == "stale"
    assert result["reason"] == "page_changed"
    assert browser.calls == []
    assert store.get_request(created["request"]["request_id"])["status"] == "stale"


def test_file_mutation_marks_stale_before_claim(tmp_path):
    document = tmp_path / "registration.pdf"
    document.write_bytes(b"v1")

    browser = FakeBrowser()
    svc, store = service(
        tmp_path,
        browser,
        allowed=[document],
    )

    created = svc.request(
        operations(document),
        requested_by="test",
    )
    approve(store, created)

    document.write_bytes(b"v2")

    result = svc.execute(created["request"]["request_id"])

    assert result["status"] == "stale"
    assert result["reason"] == "file_changed"
    assert browser.calls == []


def test_batch_mutation_marks_stale_before_claim(tmp_path):
    browser = FakeBrowser()
    svc, store = service(tmp_path, browser)

    created = svc.request(operations(), requested_by="test")
    approve(store, created)
    request_id = created["request"]["request_id"]

    row = store.get_request(request_id)
    scope = dict(row["scope"])
    scope["operations"] = [
        {
            "kind": "fill",
            "target": "organisation.name",
            "value": "MUTATED",
        },
        {"kind": "click", "target": "save"},
    ]

    with store.connect() as conn:
        conn.execute(
            "update approval_requests set scope_json = ? where request_id = ?",
            (
                json.dumps(scope, sort_keys=True, separators=(",", ":")),
                request_id,
            ),
        )

    result = svc.execute(request_id)

    assert result["status"] == "stale"
    assert result["reason"] == "scope_digest_changed"
    assert browser.calls == []


def test_submit_cannot_run_without_approval(tmp_path):
    browser = FakeBrowser()
    svc, _ = service(tmp_path, browser)

    created = svc.request(
        [{"kind": "submit", "target": FINAL_SUBMIT_TARGET}],
        requested_by="test",
    )

    result = svc.execute(created["request"]["request_id"])

    assert result["status"] == "pending"
    assert browser.calls == []


def test_success_is_exactly_once(tmp_path):
    document = tmp_path / "registration.pdf"
    document.write_bytes(b"registration")

    browser = FakeBrowser()
    svc, store = service(
        tmp_path,
        browser,
        allowed=[document],
    )

    created = svc.request(
        operations(document),
        requested_by="test",
    )
    approve(store, created)
    request_id = created["request"]["request_id"]

    first = svc.execute(request_id)
    second = svc.execute(request_id)

    assert first["status"] == "executed"
    assert first["verified"] is True
    assert second["status"] == "already_executed"
    assert [row[0] for row in browser.calls] == [
        "fill",
        "upload",
        "click",
    ]
    assert store.get_request(request_id)["status"] == "consumed"


def test_failure_or_uncertainty_is_not_retryable(tmp_path):
    browser = FakeBrowser(fail_kind="fill")
    svc, store = service(tmp_path, browser)

    created = svc.request(operations(), requested_by="test")
    approve(store, created)
    request_id = created["request"]["request_id"]

    first = svc.execute(request_id)
    second = svc.execute(request_id)

    assert first["status"] == "execution_failed"
    assert first["retry_allowed"] is False
    assert second["status"] == "execution_failed"
    assert second["retry_allowed"] is False
    assert browser.calls == [
        (
            "fill",
            "organisation.name",
            "Tiremm Innanz APS",
        )
    ]
    assert store.get_request(request_id)["status"] == "execution_failed"


def test_missing_postcondition_verifier_never_consumes_success(tmp_path):
    browser = NoVerifierBrowser()
    svc, store = service(tmp_path, browser)
    created = svc.request(operations(), requested_by="test")
    approve(store, created)

    result = svc.execute(created["request"]["request_id"])

    assert result["status"] == "execution_failed"
    assert result["retry_allowed"] is False
    assert store.get_request(created["request"]["request_id"])["status"] == "execution_failed"


def test_submit_must_be_final(tmp_path):
    svc, store = service(tmp_path)

    result = svc.request(
        [
            {"kind": "submit", "target": "save"},
            {
                "kind": "fill",
                "target": "x",
                "value": "y",
            },
        ],
        requested_by="test",
    )

    assert result["status"] == "submit_must_be_final"
    assert store.list_pending() == []


def test_unknown_submit_target_is_rejected(tmp_path):
    svc, store = service(tmp_path)

    result = svc.request(
        [{"kind": "submit", "target": "save"}],
        requested_by="test",
    )

    assert result["status"] == "final_submit_requires_separate_approval"
    assert store.list_pending() == []


def test_scope_digest_detects_recomputed_inner_batch(tmp_path):
    browser = FakeBrowser()
    svc, store = service(tmp_path, browser)
    created = svc.request(operations(), requested_by="test")
    approve(store, created)
    request_id = created["request"]["request_id"]
    scope = dict(store.get_request(request_id)["scope"])
    scope["operations"] = [
        {"kind": "fill", "target": "organisation.name", "value": "MUTATED"},
        {"kind": "click", "target": "save"},
    ]
    scope["batch_sha256"] = _batch_digest_from_scope(scope)
    with store.connect() as conn:
        conn.execute(
            "update approval_requests set scope_json = ? where request_id = ?",
            (json.dumps(scope, sort_keys=True, separators=(",", ":")), request_id),
        )

    result = svc.execute(request_id)

    assert result["status"] == "stale"
    assert result["reason"] == "scope_digest_changed"
    assert browser.calls == []


def test_action_is_real_domain_approval_action(tmp_path):
    svc, store = service(tmp_path)

    created = svc.request(operations(), requested_by="test")

    row = store.get_request(created["request"]["request_id"])
    assert row["action"] == APPROVAL_ACTION
