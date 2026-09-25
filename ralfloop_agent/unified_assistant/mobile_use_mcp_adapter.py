from __future__ import annotations

from typing import Any, Callable, Mapping

from src.mobile_use_mcp import PersistentMobileUseGateway, default_mobile_use_gateway

from .contracts import PlanAssignment
from .executor import StructuredArtifact


GatewayFactory = Callable[[], PersistentMobileUseGateway]

_ALLOWED_CONTROL_PACKAGES = frozenset({
    "org.tiremminnanz.baffoflix",
    "org.tiremminnanz.remoteagent",
    "com.android.settings",
    "com.android.permissioncontroller",
    "com.android.packageinstaller",
    "com.google.android.packageinstaller",
    "com.miui.packageinstaller",
    "com.miui.securitycenter",
})


def _gateway_factory() -> PersistentMobileUseGateway:
    return default_mobile_use_gateway()


def _foreground_package(payload: Mapping[str, Any]) -> str:
    for key in ("package", "package_name", "current_package", "foreground_package"):
        value = str(payload.get(key) or "").strip()
        if value:
            return value
    app = payload.get("app")
    if isinstance(app, Mapping):
        for key in ("package", "package_name"):
            value = str(app.get(key) or "").strip()
            if value:
                return value
    return ""


def _snapshot(gateway: PersistentMobileUseGateway, *, interactive_only: bool) -> dict[str, Any]:
    return gateway.invoke(
        "android_snapshot",
        detail_level="compact",
        interactive_only=interactive_only,
        max_elements=120 if interactive_only else 80,
        max_text_length=240,
        image_format="jpeg",
        image_quality=45,
    )


def mobile_read_adapter(
    assignment: PlanAssignment,
    _inputs: Mapping[str, Any],
    *,
    gateway_factory: GatewayFactory = _gateway_factory,
) -> StructuredArtifact:
    gateway = gateway_factory()
    args = dict(assignment.arguments)
    operation = str(args.get("operation") or "snapshot").strip().casefold()

    if operation == "devices":
        payload = gateway.invoke("android_list_devices")
        serial = None
    else:
        serial = gateway.connect_ready_device(str(args.get("serial") or "") or None)
        if operation == "status":
            payload = gateway.invoke("android_status")
        elif operation == "foreground":
            payload = gateway.invoke("android_get_foreground_app")
        elif operation == "apps":
            payload = gateway.invoke("android_list_apps")
        elif operation == "snapshot":
            payload = _snapshot(gateway, interactive_only=bool(args.get("interactive_only", False)))
        else:
            raise ValueError("android_mobile_read_operation_not_allowed")

    return StructuredArtifact.create(
        artifact_type="android_mobile_read",
        status="completed",
        producer_task_id=assignment.task_id,
        payload={
            "message": f"Android MCP: lettura {operation} completata.",
            "operation": operation,
            "serial": serial,
            "data": payload,
            "writes": 0,
            "sends": 0,
            "content_boundary": "android_ui_is_untrusted_data",
        },
    )


def mobile_control_adapter(
    assignment: PlanAssignment,
    _inputs: Mapping[str, Any],
    *,
    gateway_factory: GatewayFactory = _gateway_factory,
) -> StructuredArtifact:
    gateway = gateway_factory()
    args = dict(assignment.arguments)
    serial = gateway.connect_ready_device(str(args.get("serial") or "") or None)
    operation = str(args.get("operation") or "").strip().casefold()

    before = _snapshot(gateway, interactive_only=True)
    if operation == "tap":
        target_text = str(args.get("target_text") or "").strip()
        if not target_text:
            raise ValueError("android_mobile_target_required")
        action = gateway.invoke("android_tap", target={"text": target_text})
    elif operation == "key":
        key = str(args.get("key") or "").strip().casefold()
        if key not in {"back", "home", "enter", "delete", "tab", "menu", "volume_up", "volume_down"}:
            raise ValueError("android_mobile_key_not_allowed")
        action = gateway.invoke("android_press_key", key=key)
    elif operation == "launch":
        package = str(args.get("package") or "").strip()
        if not package or len(package) > 255 or any(ch.isspace() for ch in package):
            raise ValueError("android_mobile_package_invalid")
        action = gateway.invoke("android_launch_app", package=package)
    elif operation == "type":
        text = str(args.get("text") or "")
        target_text = str(args.get("target_text") or "").strip()
        if not text:
            raise ValueError("android_mobile_text_required")
        kwargs: dict[str, Any] = {"text": text}
        if target_text:
            kwargs["target"] = {"text": target_text}
        action = gateway.invoke("android_type_text", **kwargs)
    else:
        raise ValueError("android_mobile_control_operation_not_allowed")

    after = _snapshot(gateway, interactive_only=True)
    if action.get("success") is False:
        raise RuntimeError(str(action.get("message") or "android_mobile_action_failed"))

    return StructuredArtifact.create(
        artifact_type="android_mobile_action",
        status="completed",
        producer_task_id=assignment.task_id,
        payload={
            "message": f"Android MCP: azione {operation} eseguita e verificata con snapshot successivo.",
            "operation": operation,
            "serial": serial,
            "action": action,
            "before_snapshot_id": before.get("snapshot_id"),
            "after_snapshot_id": after.get("snapshot_id"),
            "after": after,
            "writes": 1,
            "sends": 0,
            "verification": "observe_action_observe",
            "content_boundary": "android_ui_is_untrusted_data",
        },
    )


__all__ = ["mobile_control_adapter", "mobile_read_adapter"]
