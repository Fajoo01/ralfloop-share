from pathlib import Path
import socket
import subprocess

import pytest

from src.mcp_transport import MCPClientSession, MCPError, StdioMCPTransport


ROOT = Path(__file__).resolve().parents[1]
POOL_DIR = ROOT / "tools" / "teacher_model_pool_mcp"
BINARY = POOL_DIR / "ralf-teacher-model-pool-mcp"


def _pool_session(tmp_path: Path, *, capacity: int = 1):
    subprocess.run(["make", "-C", str(POOL_DIR)], check=True, capture_output=True)
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(8)
    port = listener.getsockname()[1]
    config = tmp_path / "pool.conf"
    config.write_text(f"sibilla 127.0.0.1 {port} {capacity}\n", encoding="utf-8")
    session = MCPClientSession(
        StdioMCPTransport([str(BINARY), "--config", str(config), "--stdio"]),
        timeout=3,
        client_name="teacher-pool-test",
    )
    return listener, port, session


def _payload(raw):
    value = raw.get("structuredContent")
    assert isinstance(value, dict)
    assert value["ok"] is True
    assert value["writes"] == 0
    assert value["external_side_effects"] == 0
    return value


def test_model_pool_mcp_bounds_leases_and_keeps_endpoint_fixed(tmp_path):
    listener, port, session = _pool_session(tmp_path)
    try:
        with listener, session:
            names = {tool.name for tool in session.list_tools()}
            assert names == {"pool.acquire", "pool.release", "pool.health"}

            health = _payload(session.call_tool("pool.health", {}))
            assert health["nodes"] == [{
                "name": "sibilla",
                "healthy": True,
                "inflight": 0,
                "capacity": 1,
                "ema_ms": 0.0,
            }]

            acquired = _payload(session.call_tool(
                "pool.acquire", {"model": "gemma3:4b"}
            ))
            assert acquired["node"] == "sibilla"
            assert acquired["endpoint"] == f"http://127.0.0.1:{port}"
            assert acquired["inflight"] == 1
            lease = acquired["lease"]

            with pytest.raises(MCPError):
                session.call_tool("pool.acquire", {"model": "gemma3:4b"})
            with pytest.raises(MCPError):
                session.call_tool("pool.acquire", {"model": "qwen2.5:3b"})

            released = _payload(session.call_tool(
                "pool.release", {"lease": lease, "latency_ms": 123.0}
            ))
            assert released["inflight"] == 0
            assert released["ema_ms"] == 123.0

            reacquired = _payload(session.call_tool(
                "pool.acquire", {"model": "gemma3:4b"}
            ))
            assert reacquired["inflight"] == 1
    finally:
        session.close()
        listener.close()
