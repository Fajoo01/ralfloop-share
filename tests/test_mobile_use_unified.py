from __future__ import annotations

from contextlib import contextmanager

import pytest

from ralfloop_agent.unified_assistant.mobile_use_mcp_adapter import mobile_control_adapter, mobile_read_adapter
from ralfloop_agent.unified_assistant.planner import UnifiedPlanner
from ralfloop_agent.unified_assistant.registry import UnifiedRegistryFacade


class FakeGateway:
    def __init__(self):
        self.calls = []

    def connect_ready_device(self, serial=None):
        self.calls.append(("connect_ready_device", serial))
        return serial or "redmi:5555"

    def invoke(self, name, **kwargs):
        self.calls.append((name, kwargs))
        if name == "android_list_devices":
            return {"success": True, "devices": [{"serial": "redmi:5555", "state": "device"}]}
        if name == "android_snapshot":
            return {"success": True, "snapshot_id": f"s{len(self.calls)}", "elements": []}
        if name == "android_get_foreground_app":
            return {"success": True, "package": "org.tiremminnanz.baffoflix"}
        return {"success": True}


def test_planner_routes_redmi_snapshot_read_only():
    planner = UnifiedPlanner(UnifiedRegistryFacade())
    plan = planner.validate(planner.plan("Controlla lo schermo del Redmi"))
    item = plan.assignments[0]
    assert plan.intent == "android.mobile.read"
    assert item.skill == "android.mobile.read"
    assert item.arguments == {"operation": "snapshot"}
    assert item.policy.value == "READ"


def test_planner_routes_semantic_tap_without_coordinates():
    planner = UnifiedPlanner(UnifiedRegistryFacade())
    plan = planner.validate(planner.plan("Sul Redmi tocca Installa"))
    item = plan.assignments[0]
    assert plan.intent == "android.mobile.control"
    assert item.arguments == {"operation": "tap", "target_text": "Installa"}
    assert item.policy.value == "AUTO_WRITE"


def test_mobile_control_observes_before_and_after_action():
    planner = UnifiedPlanner(UnifiedRegistryFacade())
    assignment = planner.validate(planner.plan("Sul Redmi tocca Installa")).assignments[0]
    fake = FakeGateway()
    artifact = mobile_control_adapter(assignment, {"user.goal": assignment.objective}, gateway_factory=lambda: fake)
    names = [row[0] for row in fake.calls]
    assert names == ["connect_ready_device", "android_snapshot", "android_tap", "android_snapshot"]
    tap_args = fake.calls[2][1]
    assert tap_args == {"target": {"text": "Installa"}}
    assert artifact.status == "completed"
    assert artifact.payload["verification"] == "observe_action_observe"
    assert artifact.payload["writes"] == 1


def test_mobile_read_is_zero_write():
    planner = UnifiedPlanner(UnifiedRegistryFacade())
    assignment = planner.validate(planner.plan("Controlla lo schermo del Redmi")).assignments[0]
    fake = FakeGateway()
    artifact = mobile_read_adapter(assignment, {"user.goal": assignment.objective}, gateway_factory=lambda: fake)
    assert artifact.status == "completed"
    assert artifact.payload["writes"] == 0
    assert artifact.payload["sends"] == 0


def test_mobile_control_rejects_unallowlisted_launch_package():
    planner = UnifiedPlanner(UnifiedRegistryFacade())
    base = planner.validate(planner.plan("Sul Redmi tocca Installa")).assignments[0]
    assignment = base.model_copy(update={"arguments": {"operation": "launch", "package": "com.example.untrusted"}})
    fake = FakeGateway()
    with pytest.raises(ValueError, match="android_mobile_package_not_allowed"):
        mobile_control_adapter(assignment, {"user.goal": assignment.objective}, gateway_factory=lambda: fake)
