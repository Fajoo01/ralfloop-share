from __future__ import annotations

import os
import threading
from typing import Any, Mapping

from src.mcp_transport import MCPClientSession, MCPError, MCPProtocolError, UnixMCPTransport

DEFAULT_SOCKET = "/run/ralf-mobile-use-mcp/mcp.sock"
READ_TOOLS = frozenset({
    "android_list_devices", "android_status", "android_snapshot", "android_screenshot",
    "android_get_ui_elements", "android_get_foreground_app", "android_list_apps",
})
CONTROL_TOOLS = frozenset({
    "android_connect", "android_disconnect", "android_tap", "android_long_press",
    "android_swipe", "android_type_text", "android_clear_text", "android_press_key",
    "android_launch_app", "android_terminate_app", "android_open_url", "android_wait",
})
RECORD_TOOLS = frozenset({"android_start_recording", "android_stop_recording"})
ALLOWED_TOOLS = READ_TOOLS | CONTROL_TOOLS | RECORD_TOOLS


class PersistentMobileUseGateway:
    """Serialized resident client for the local mobile-use MCP broker."""

    def __init__(self, socket_path: str | None = None, timeout: float | None = None) -> None:
        self.socket_path = socket_path or os.getenv("RALF_MOBILE_USE_MCP_SOCKET", DEFAULT_SOCKET)
        self.timeout = float(timeout or os.getenv("RALF_MOBILE_USE_MCP_TIMEOUT", "15"))
        self._lock = threading.RLock()
        self._session: MCPClientSession | None = None
        self._tools: set[str] = set()
        self._connected_serial: str | None = None

    def _close_locked(self) -> None:
        session, self._session = self._session, None
        self._tools = set()
        self._connected_serial = None
        if session is not None:
            try:
                session.__exit__(None, None, None)
            except Exception:
                pass

    def close(self) -> None:
        with self._lock:
            self._close_locked()

    def _ensure_locked(self) -> MCPClientSession:
        if self._session is not None:
            return self._session
        session = MCPClientSession(
            UnixMCPTransport(self.socket_path, connect_timeout=0.8),
            timeout=self.timeout,
            client_name="ralf-mobile-use-resident",
        )
        try:
            session.__enter__()
            tools = {tool.name for tool in session.list_tools()}
            missing = READ_TOOLS - tools
            if missing:
                raise MCPProtocolError("mobile_use_missing_read_tools:" + ",".join(sorted(missing)))
        except Exception:
            try:
                session.__exit__(None, None, None)
            except Exception:
                pass
            raise
        self._session = session
        self._tools = tools
        return session

    def invoke(self, name: str, **arguments: Any) -> dict[str, Any]:
        if name not in ALLOWED_TOOLS:
            raise ValueError("mobile_use_tool_not_allowed")
        with self._lock:
            session = self._ensure_locked()
            if name not in self._tools:
                raise MCPProtocolError("mobile_use_tool_not_discovered:" + name)
            try:
                result = session.call_tool(name, arguments)
            except Exception:
                # Never retry actions: transport failure after dispatch is outcome-uncertain.
                self._close_locked()
                raise
            payload = result.get("structuredContent") if isinstance(result, Mapping) else None
            if not isinstance(payload, Mapping):
                raise MCPProtocolError("mobile_use_invalid_payload")
            return dict(payload)

    def connect_ready_device(self, serial: str | None = None) -> str:
        requested = (serial or os.getenv("RALF_ANDROID_DEVICE_SERIAL", "")).strip()
        with self._lock:
            inventory = self.invoke("android_list_devices")
            devices = [row for row in inventory.get("devices", ()) if isinstance(row, Mapping)]
            online = [row for row in devices if str(row.get("state") or "") == "device"]
            if requested:
                selected = next((row for row in online if str(row.get("serial") or "") == requested), None)
                if selected is None:
                    raise MCPError("android_requested_device_not_online")
            elif len(online) == 1:
                selected = online[0]
            elif not online:
                raise MCPError("android_no_online_device")
            else:
                raise MCPError("android_multiple_online_devices")
            selected_serial = str(selected.get("serial") or "")
            if not selected_serial:
                raise MCPProtocolError("android_device_serial_missing")
            if self._connected_serial != selected_serial:
                connected = self.invoke("android_connect", serial=selected_serial)
                if connected.get("success") is False:
                    raise MCPError(str(connected.get("message") or "android_connect_failed"))
                self._connected_serial = selected_serial
            return selected_serial


_DEFAULT_GATEWAY: PersistentMobileUseGateway | None = None
_DEFAULT_GATEWAY_LOCK = threading.Lock()


def default_mobile_use_gateway() -> PersistentMobileUseGateway:
    global _DEFAULT_GATEWAY
    with _DEFAULT_GATEWAY_LOCK:
        if _DEFAULT_GATEWAY is None:
            _DEFAULT_GATEWAY = PersistentMobileUseGateway()
        return _DEFAULT_GATEWAY


__all__ = [
    "ALLOWED_TOOLS", "CONTROL_TOOLS", "DEFAULT_SOCKET", "MCPError",
    "PersistentMobileUseGateway", "READ_TOOLS", "RECORD_TOOLS",
    "default_mobile_use_gateway",
]
