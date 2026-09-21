#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import time

from src.mcp_transport import MCPClientSession, UnixMCPTransport


def timed(label, func):
    started = time.perf_counter()
    value = func()
    elapsed_ms = (time.perf_counter() - started) * 1000
    print(json.dumps({"step": label, "latency_ms": round(elapsed_ms, 1), "ok": True}))
    return value


def preview(result):
    content = result.get("content") or []
    text = "\n".join(str(row.get("text", "")) for row in content if isinstance(row, dict))
    return text[:1000].replace("\n", " ")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--socket", default="/run/ralf-browser-playwright-mcp/mcp.sock")
    parser.add_argument("--timeout", type=float, default=30.0)
    args = parser.parse_args()
    session = MCPClientSession(
        UnixMCPTransport(args.socket), timeout=args.timeout, client_name="browser-benchmark"
    )
    try:
        timed("initialize", session.initialize)
        tools = timed("tools_list", session.list_tools)
        names = {tool.name for tool in tools}
        print(json.dumps({"tool_count": len(names), "unsafe_present": "browser_run_code_unsafe" in names}))
        tabs = timed("browser_tabs", lambda: session.call_tool("browser_tabs", {"action": "list"}))
        print(json.dumps({"browser_tabs_preview": preview(tabs)}, ensure_ascii=False))
        snapshot = timed("browser_snapshot", lambda: session.call_tool("browser_snapshot", {}))
        print(json.dumps({"browser_snapshot_preview": preview(snapshot)}, ensure_ascii=False))
    finally:
        session.close()


if __name__ == "__main__":
    main()
