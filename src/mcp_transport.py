from __future__ import annotations

"""Small, generic MCP 2025-03-26 JSON-RPC client.

The client deliberately accepts argv as trusted configuration only.  Tool/model
output can select a discovered tool and validated arguments, but can never add a
process, shell command, environment variable, or transport endpoint.
"""

from dataclasses import dataclass
import json
import os
import selectors
import socket
import subprocess
import threading
import time
from typing import Any, BinaryIO, Mapping, Protocol, Sequence


MCP_PROTOCOL_VERSION = "2025-03-26"


class MCPError(RuntimeError):
    pass


class MCPTimeout(MCPError):
    pass


class MCPProcessDied(MCPError):
    pass


class MCPProtocolError(MCPError):
    pass


class MCPTransport(Protocol):
    def send(self, message: Mapping[str, Any]) -> None: ...
    def receive(self, timeout: float) -> dict[str, Any]: ...
    def close(self) -> None: ...


class _LineTransport:
    def __init__(self, reader: BinaryIO, writer: BinaryIO) -> None:
        self.reader = reader
        self.writer = writer
        self._buffer = bytearray()
        self._lock = threading.Lock()

    def send(self, message: Mapping[str, Any]) -> None:
        payload = json.dumps(dict(message), ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n"
        with self._lock:
            try:
                self.writer.write(payload)
                self.writer.flush()
            except (BrokenPipeError, OSError) as exc:
                raise MCPProcessDied("mcp_transport_closed") from exc

    def receive(self, timeout: float) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        while True:
            newline = self._buffer.find(b"\n")
            if newline >= 0:
                raw = bytes(self._buffer[:newline]).rstrip(b"\r")
                del self._buffer[: newline + 1]
                if not raw:
                    continue
                if len(raw) > 8 * 1024 * 1024:
                    raise MCPProtocolError("mcp_message_too_large")
                try:
                    value = json.loads(raw)
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise MCPProtocolError("malformed_mcp_response") from exc
                if not isinstance(value, dict):
                    raise MCPProtocolError("mcp_response_not_object")
                return value
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise MCPTimeout("mcp_receive_timeout")
            selector = selectors.DefaultSelector()
            try:
                selector.register(self.reader, selectors.EVENT_READ)
                if not selector.select(remaining):
                    raise MCPTimeout("mcp_receive_timeout")
            finally:
                selector.close()
            chunk = os.read(self.reader.fileno(), 65536)
            if not chunk:
                raise MCPProcessDied("mcp_transport_eof")
            self._buffer.extend(chunk)


class StdioMCPTransport(_LineTransport):
    def __init__(self, argv: Sequence[str], *, env: Mapping[str, str] | None = None, cwd: str | None = None) -> None:
        if not argv or any(not isinstance(part, str) or not part for part in argv):
            raise ValueError("trusted_nonempty_argv_required")
        self.argv = tuple(argv)
        self.process = subprocess.Popen(
            self.argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            shell=False,
            env=dict(env) if env is not None else None,
            cwd=cwd,
            start_new_session=True,
        )
        assert self.process.stdin is not None and self.process.stdout is not None
        super().__init__(self.process.stdout, self.process.stdin)

    def receive(self, timeout: float) -> dict[str, Any]:
        try:
            return super().receive(timeout)
        except MCPTimeout:
            if self.process.poll() is not None:
                raise MCPProcessDied(f"mcp_process_exit:{self.process.returncode}")
            raise

    def close(self) -> None:
        if self.process.poll() is not None:
            return
        try:
            self.process.terminate()
            self.process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=2)
        finally:
            for stream in (self.process.stdin, self.process.stdout):
                if stream:
                    stream.close()


class UnixMCPTransport(_LineTransport):
    """MCP JSON-RPC over an AF_UNIX byte stream (same message framing as stdio)."""

    def __init__(self, socket_path: str, *, connect_timeout: float = 3.0) -> None:
        self.socket_path = socket_path
        self.socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.socket.settimeout(connect_timeout)
        try:
            self.socket.connect(socket_path)
        except OSError as exc:
            self.socket.close()
            raise MCPProcessDied("mcp_broker_unavailable") from exc
        self.socket.settimeout(None)
        reader = self.socket.makefile("rb", buffering=0)
        writer = self.socket.makefile("wb", buffering=0)
        super().__init__(reader, writer)

    def close(self) -> None:
        try:
            self.writer.close()
            self.reader.close()
        finally:
            self.socket.close()


@dataclass(frozen=True)
class MCPTool:
    name: str
    description: str
    input_schema: Mapping[str, Any]


class MCPClientSession:
    def __init__(self, transport: MCPTransport, *, timeout: float = 20.0, client_name: str = "ralf") -> None:
        self.transport = transport
        self.timeout = timeout
        self.client_name = client_name
        self._next_id = 1
        self._initialized = False
        self._tools: dict[str, MCPTool] = {}

    def __enter__(self) -> "MCPClientSession":
        self.initialize()
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def initialize(self) -> Mapping[str, Any]:
        result = self._request("initialize", {
            "protocolVersion": MCP_PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": self.client_name, "version": "1"},
        })
        version = str(result.get("protocolVersion") or "")
        if not version:
            raise MCPProtocolError("initialize_missing_protocol_version")
        self.transport.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        self._initialized = True
        return result

    def list_tools(self) -> tuple[MCPTool, ...]:
        if not self._initialized:
            raise MCPProtocolError("mcp_not_initialized")
        cursor: str | None = None
        tools: dict[str, MCPTool] = {}
        while True:
            params = {"cursor": cursor} if cursor else {}
            result = self._request("tools/list", params)
            raw_tools = result.get("tools")
            if not isinstance(raw_tools, list):
                raise MCPProtocolError("malformed_tools_list")
            for item in raw_tools:
                if not isinstance(item, dict) or not isinstance(item.get("name"), str) or not isinstance(item.get("inputSchema"), dict):
                    raise MCPProtocolError("malformed_tool_definition")
                tool = MCPTool(item["name"], str(item.get("description") or ""), item["inputSchema"])
                tools[tool.name] = tool
            next_cursor = result.get("nextCursor")
            if not next_cursor:
                break
            if not isinstance(next_cursor, str) or next_cursor == cursor:
                raise MCPProtocolError("invalid_tools_cursor")
            cursor = next_cursor
        self._tools = tools
        return tuple(tools.values())

    def call_tool(self, name: str, arguments: Mapping[str, Any]) -> Mapping[str, Any]:
        if name not in self._tools:
            raise MCPProtocolError("undiscovered_tool")
        result = self._request("tools/call", {"name": name, "arguments": dict(arguments)})
        content = result.get("content")
        structured = result.get("structuredContent")
        if content is not None and not isinstance(content, list):
            raise MCPProtocolError("malformed_tool_content")
        if structured is not None and not isinstance(structured, dict):
            raise MCPProtocolError("malformed_structured_content")
        if result.get("isError"):
            raise MCPError(_content_error(content) or "mcp_tool_error")
        if isinstance(structured, dict) and structured.get("ok") is False:
            error = structured.get("error") or structured.get("message") or "mcp_tool_reported_failure"
            raise MCPError(str(error))
        return result

    def close(self) -> None:
        self.transport.close()
        self._initialized = False

    def _request(self, method: str, params: Mapping[str, Any]) -> Mapping[str, Any]:
        request_id = self._next_id
        self._next_id += 1
        self.transport.send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": dict(params)})
        deadline = time.monotonic() + self.timeout
        while True:
            response = self.transport.receive(max(0.001, deadline - time.monotonic()))
            if "method" in response and "id" not in response:
                continue
            if response.get("jsonrpc") != "2.0" or response.get("id") != request_id:
                raise MCPProtocolError("mcp_response_id_mismatch")
            if "error" in response:
                error = response["error"]
                raise MCPError(str(error.get("message") if isinstance(error, dict) else error))
            result = response.get("result")
            if not isinstance(result, dict):
                raise MCPProtocolError("mcp_result_not_object")
            return result


def _content_error(content: Any) -> str:
    if isinstance(content, list):
        return " ".join(str(item.get("text")) for item in content if isinstance(item, dict) and item.get("type") == "text")
    return ""
