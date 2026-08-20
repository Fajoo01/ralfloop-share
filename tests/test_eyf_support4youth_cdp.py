from __future__ import annotations

import asyncio

import pytest
from fastapi import FastAPI

from ralfloop_agent.integration import eyf_support4youth_cdp as cdp_module
from ralfloop_agent.domains.domain_approval import DomainApprovalPolicy
from ralfloop_agent.domains.domain_approval_store import DomainApprovalStore
from ralfloop_agent.integration.eyf_support4youth_cdp import (
    CdpPage,
    EyfSupport4YouthCdpAdapter,
    EyfSupport4YouthError,
    FIELD_REGISTRY,
    FIELD_SPECS,
    FINAL_SUBMIT_TARGET,
    make_eyf_support4youth_service,
    register_eyf_support4youth_routes,
)


URL = "https://support4youth.coe.int/organization/profile"
NAME_KEY = "generalInformation.organizationNameRegistered"
BOOL_KEY = "organizationInBrief.primaryMissionYoungPeople"


class FakeTransport:
    def __init__(self, pages=None):
        self.inventory = list(
            pages
            or [CdpPage(target_id="target-1", title="Support4Youth", url=URL)]
        )
        self.calls = []
        self.values = {spec.key: "-" for spec in FIELD_SPECS}
        self.pending = 0

    def pages(self):
        return tuple(self.inventory)

    def call_function(self, page, function, arguments, *, operation):
        self.calls.append((page.target_id, operation, arguments))
        if operation.endswith("snapshot"):
            return {
                "ok": True,
                "url": page.url,
                "title": page.title,
                "stable": {
                    "fields": [
                        {
                            "key": spec.key,
                            "label": spec.local_name,
                            "value": self.values[spec.key],
                            "comments": [],
                            "reply_value": "",
                            "dom": {
                                "label": True,
                                "text": True,
                                "edit": True,
                                "reply_input": True,
                                "reply_save": True,
                            },
                        }
                        for spec in FIELD_SPECS
                    ],
                    "alerts": [],
                    "documents": [],
                    "pendingUpdatesCount": self.pending,
                    "finalDeclarationAvailable": self.pending > 0,
                    "workflow": {
                        "dom_ready": True,
                        "field_dom_count": len(FIELD_SPECS),
                        "clarification_keys": [
                            spec.key for spec in FIELD_SPECS if spec.required
                        ],
                        "unresolved_clarification_keys": [
                            spec.key
                            for spec in FIELD_SPECS
                            if spec.required
                            and self.values[spec.key] in {"", "-"}
                        ],
                        "actions_button_available": self.pending > 0,
                        "pending_updates_count": self.pending,
                        "send_updates_available": self.pending > 0,
                        "registration_status": "clarification requested",
                    },
                },
            }
        return {"ok": True, "action": arguments[0]}


def _policy(tmp_path):
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


def _service(tmp_path, transport):
    policy = _policy(tmp_path)
    return make_eyf_support4youth_service(
        policy=policy,
        store=DomainApprovalStore(policy=policy),
        transport=transport,
        approval_outbox=tmp_path / "approval-outbox.jsonl",
    )


def _name_batch(value="Example NGO"):
    return [
        {"kind": "click", "target": f"field:{NAME_KEY}:edit"},
        {"kind": "fill", "target": f"field:{NAME_KEY}", "value": value},
        {"kind": "click", "target": f"field:{NAME_KEY}:save"},
    ]


def test_registry_is_fixed_22_field_contract():
    assert len(FIELD_SPECS) == len(FIELD_REGISTRY) == 22
    assert sum(spec.kind == "frequency" for spec in FIELD_SPECS) == 8
    assert sum(spec.kind == "boolean" for spec in FIELD_SPECS) == 9
    assert {spec.kind for spec in FIELD_SPECS} == {
        "text",
        "boolean",
        "integer",
        "frequency",
        "multi_choice",
    }


def test_mutation_function_has_in_dom_route_guard_and_real_final_selectors():
    source = cdp_module._ACTION_FUNCTION

    assert "assertProfileLocation();" in source
    assert "location.hostname !== 'support4youth.coe.int'" in source
    assert "document.querySelector('eyf-info-action-banner')" in source
    assert "document.querySelector('eyf-declaration-modal')" in source
    assert "getElementById('eyf-declaration-modal')" not in source
    assert ".p-timeline-event-content p.w-full" in cdp_module._SNAPSHOT_FUNCTION
    assert "declaration_choices_unavailable" in source
    assert "edit-field-dialog-body" in cdp_module._SNAPSHOT_FUNCTION


def test_field_save_scopes_duplicate_id_to_active_dialog():
    source = cdp_module._ACTION_FUNCTION

    assert "const buttonFor = (id, root = document)" in source
    assert "const activeField = document.getElementById(base)" in source
    assert (
        "const dialog = activeField.closest('[role=\"dialog\"],.p-dialog')"
        in source
    )
    assert "buttonFor('edit-field-save', dialog).click()" in source
    assert "buttonFor('edit-field-save').click()" not in source
    assert "!activeField.isConnected || !dialog.isConnected" in source


def test_snapshot_uses_pending_replacement_not_struck_old_value():
    source = cdp_module._SNAPSHOT_FUNCTION

    assert "const fieldValue = (node)" in source
    assert "child.classList.contains('line-through')" in source
    assert "!child.classList.contains('line-through')" in source
    assert "value: fieldValue(text).slice(0, 2000)" in source


def test_snapshot_is_read_only_stable_and_target_bound():
    transport = FakeTransport()
    adapter = EyfSupport4YouthCdpAdapter(transport)

    result = adapter.snapshot()

    assert result["target_id"] == "target-1"
    assert [row["key"] for row in result["stable"]["fields"]] == [
        spec.key for spec in FIELD_SPECS
    ]
    assert result["stable"]["pendingUpdatesCount"] == 0
    assert result["stable"]["documents"] == []
    assert [call[1] for call in transport.calls] == [
        "eyf.support4youth.snapshot"
    ]


@pytest.mark.parametrize(
    "url",
    [
        "http://support4youth.coe.int/organization/profile",
        "https://support4youth.coe.int.evil.invalid/organization/profile",
        "https://support4youth.coe.int/organisation/profile",
        "https://support4youth.coe.int/organization/profile?next=1",
        "https://support4youth.coe.int/organization/profile#x",
    ],
)
def test_origin_and_exact_path_fail_closed(url):
    adapter = EyfSupport4YouthCdpAdapter(
        FakeTransport([CdpPage("bad", "bad", url)])
    )
    with pytest.raises(EyfSupport4YouthError, match="target_missing"):
        adapter.snapshot()


def test_expected_target_and_stale_target_never_mutate():
    transport = FakeTransport()
    adapter = EyfSupport4YouthCdpAdapter(
        transport,
        expected_target_id="target-1",
    )
    adapter.snapshot()
    transport.inventory.clear()

    with pytest.raises(EyfSupport4YouthError, match="target_stale"):
        adapter.fill(f"field:{NAME_KEY}", "Example NGO")

    assert all(call[1].endswith("snapshot") for call in transport.calls)


def test_raw_selector_and_index_targets_rejected_without_cdp_action():
    transport = FakeTransport()
    adapter = EyfSupport4YouthCdpAdapter(transport)

    with pytest.raises(EyfSupport4YouthError, match="semantic_target_forbidden"):
        adapter.fill("#edit-field-organizationNameRegistered", "Example NGO")
    with pytest.raises(EyfSupport4YouthError, match="semantic_target_forbidden"):
        adapter.click("field:0:save")

    assert transport.calls == []


def test_batch_state_machine_and_final_separation():
    adapter = EyfSupport4YouthCdpAdapter(FakeTransport())
    adapter.validate_batch(_name_batch())
    adapter.validate_batch(
        [{"kind": "submit", "target": FINAL_SUBMIT_TARGET}]
    )

    with pytest.raises(EyfSupport4YouthError, match="field_state_machine_invalid"):
        adapter.validate_batch(_name_batch()[:2])
    with pytest.raises(EyfSupport4YouthError, match="save_batch_state_machine_invalid"):
        adapter.validate_batch(
            _name_batch()
            + [{"kind": "submit", "target": FINAL_SUBMIT_TARGET}]
        )


def test_semantic_calls_never_forward_selectors_or_indexes():
    transport = FakeTransport()
    adapter = EyfSupport4YouthCdpAdapter(transport)

    adapter.click(f"field:{NAME_KEY}:edit")
    adapter.fill(f"field:{NAME_KEY}", "Example NGO")
    adapter.click(f"field:{NAME_KEY}:save")

    arguments = [call[2] for call in transport.calls]
    assert arguments == [
        ("field_edit", NAME_KEY, ""),
        ("field_fill", NAME_KEY, "Example NGO"),
        ("field_save", NAME_KEY, ""),
    ]
    assert not any("#" in str(argument) for row in arguments for argument in row)


def test_service_distinguishes_save_batch_and_final_approval(tmp_path):
    transport = FakeTransport()
    service = _service(tmp_path, transport)

    save = service.preview(_name_batch())
    transport.values = {spec.key: "answered" for spec in FIELD_SPECS}
    transport.pending = 1
    final = service.preview(
        [{"kind": "submit", "target": FINAL_SUBMIT_TARGET}]
    )
    requested = service.request(
        [{"kind": "submit", "target": FINAL_SUBMIT_TARGET}],
        requested_by="test",
    )

    assert save["approval_phase"] == "save_batch"
    assert final["approval_phase"] == "final_submission"
    assert save["batch_sha256"] != final["batch_sha256"]
    assert requested["request"]["scope"]["final_declarations"] == {
        "accept_terms": True,
        "accept_data_processing": True,
    }
    message = requested["request"]["telegram_message"]
    assert "FINAL SUBMISSION: Send updates" in message
    assert "accettazione termini" in message
    assert "accettazione trattamento dati" in message


def test_postcondition_checks_value_pending_and_final_clear():
    adapter = EyfSupport4YouthCdpAdapter(FakeTransport())
    fields = [
        {"key": spec.key, "value": "Example NGO" if spec.key == NAME_KEY else "-", "reply_value": ""}
        for spec in FIELD_SPECS
    ]
    before = {
        "fields": fields,
        "workflow": {"dom_ready": True, "pending_updates_count": 0, "send_updates_available": False, "registration_status": "clarification requested"},
    }
    after = {
        "fields": fields,
        "workflow": {"dom_ready": True, "pending_updates_count": 1, "send_updates_available": True, "registration_status": "clarification requested"},
    }
    assert adapter.verify_postconditions(_name_batch(), before, after)["ok"] is True

    final_after = {
        "fields": fields,
        "workflow": {"dom_ready": True, "pending_updates_count": 0, "send_updates_available": False, "registration_status": "submitted"},
    }
    assert adapter.verify_postconditions(
        [{"kind": "submit", "target": FINAL_SUBMIT_TARGET}],
        after,
        final_after,
    )["ok"] is True


def test_final_postcondition_rejects_blank_unknown_dom():
    adapter = EyfSupport4YouthCdpAdapter(FakeTransport())
    operation = [{"kind": "submit", "target": FINAL_SUBMIT_TARGET}]

    result = adapter.verify_postconditions(
        operation,
        {"workflow": {"dom_ready": True, "pending_updates_count": 1}},
        {
            "workflow": {
                "dom_ready": False,
                "pending_updates_count": 0,
                "send_updates_available": False,
                "registration_status": "",
            }
        },
    )

    assert result["ok"] is False


def test_reply_postcondition_accepts_official_no_id_timeline_message():
    adapter = EyfSupport4YouthCdpAdapter(FakeTransport())
    reply = "Updated from official ARCI registry."
    operation = [{"kind": "fill", "target": f"reply:{NAME_KEY}", "value": reply}]
    before = {
        "fields": [{"key": NAME_KEY, "comments": ["please answer"]}],
        "workflow": {"dom_ready": True},
    }
    after = {
        "fields": [{
            "key": NAME_KEY,
            "comments": ["please answer", reply],
            "reply_value": "",
        }],
        "workflow": {"dom_ready": True},
    }

    result = adapter.verify_postconditions(operation, before, after)

    assert result["ok"] is True
    assert result["verified_reply_count"] == 1


def test_reply_postcondition_rejects_unrelated_new_comment():
    adapter = EyfSupport4YouthCdpAdapter(FakeTransport())
    operation = [{
        "kind": "fill",
        "target": f"reply:{NAME_KEY}",
        "value": "Approved exact reply",
    }]
    before = {
        "fields": [{"key": NAME_KEY, "comments": ["please answer"]}],
        "workflow": {"dom_ready": True},
    }
    after = {
        "fields": [{
            "key": NAME_KEY,
            "comments": ["please answer", "different concurrent comment"],
            "reply_value": "",
        }],
        "workflow": {"dom_ready": True},
    }

    result = adapter.verify_postconditions(operation, before, after)

    assert result["ok"] is False
    assert result["unverified_reply_keys"] == [NAME_KEY]


def test_final_preflight_requires_complete_ready_profile():
    adapter = EyfSupport4YouthCdpAdapter(FakeTransport())
    operation = [{"kind": "submit", "target": FINAL_SUBMIT_TARGET}]
    state = {
        "fields": [],
        "finalDeclarationAvailable": True,
        "workflow": {
            "dom_ready": True,
            "clarification_keys": [NAME_KEY],
            "unresolved_clarification_keys": [NAME_KEY],
            "send_updates_available": True,
            "pending_updates_count": 1,
            "edit_dialog_open": False,
            "declaration_modal_open": False,
        },
    }

    with pytest.raises(EyfSupport4YouthError, match="final_submission_not_ready"):
        adapter.validate_snapshot_for_operations(operation, state)


class FakeRouteService:
    class Browser:
        def snapshot(self):
            return {"url": URL, "stable": {}}

    def __init__(self):
        self.browser = self.Browser()
        self.calls = []

    def preview(self, operations):
        self.calls.append(("preview", list(operations)))
        return {"status": "preview"}

    def request(self, operations, *, requested_by):
        self.calls.append(("request", list(operations), requested_by))
        return {"status": "pending"}

    def execute(self, request_id):
        self.calls.append(("execute", request_id))
        return {"status": "executed"}


def test_route_factory_matches_cli_and_binds_final_operation():
    app = FastAPI()
    service = FakeRouteService()
    register_eyf_support4youth_routes(app, service=service)
    routes = {route.path: route.endpoint for route in app.routes}

    assert "/portals/support4youth/snapshot" in routes
    assert "/portals/support4youth/preview" in routes
    assert "/portals/support4youth/requests" in routes
    assert "/portals/support4youth/requests/{request_id}/apply" in routes

    asyncio.run(
        routes["/portals/support4youth/send-updates/requests"](
            {"requested_by": "test"}
        )
    )
    assert service.calls[-1] == (
        "request",
        [{"kind": "submit", "target": FINAL_SUBMIT_TARGET}],
        "test",
    )
