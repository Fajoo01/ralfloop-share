#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import json
from typing import Any

from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client


async def _call(url: str, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
    async with streamable_http_client(url) as streams:
        read_stream, write_stream = streams[0], streams[1]
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            listed = await session.list_tools()
            tools = {item.name: item for item in listed.tools}
            names = set(tools)
            if tool not in names:
                return {
                    "ok": False,
                    "error": "tool_not_found",
                    "available": [
                        {"name": name, "inputSchema": getattr(tools[name], "input_schema", {})}
                        for name in sorted(names)
                    ],
                }
            result = await session.call_tool(tool, arguments)
            if getattr(result, "is_error", False):
                text = " ".join(
                    str(getattr(item, "text", ""))
                    for item in getattr(result, "content", ())
                    if getattr(item, "type", "") == "text"
                ).strip()
                return {"ok": False, "error": text or "remote_tool_error"}
            structured = getattr(result, "structured_content", None)
            if structured is not None:
                return {"ok": True, "result": structured}
            content = []
            for item in getattr(result, "content", ()):
                if getattr(item, "type", "") == "text":
                    raw = str(getattr(item, "text", ""))
                    try:
                        content.append(json.loads(raw))
                    except json.JSONDecodeError:
                        content.append(raw)
            return {"ok": True, "result": content[0] if len(content) == 1 else content}


def main() -> int:
    parser = argparse.ArgumentParser(description="Bounded MCP client for the local Tiremm Android bridge")
    parser.add_argument("--url", default="http://127.0.0.1:19232/mcp")
    parser.add_argument("--tool", required=True)
    parser.add_argument("--arguments-json", default="{}")
    args = parser.parse_args()
    try:
        payload = json.loads(args.arguments_json)
        if not isinstance(payload, dict):
            raise ValueError("arguments_must_be_object")
        result = asyncio.run(_call(args.url, args.tool, payload))
    except Exception as exc:
        result = {"ok": False, "error": f"{type(exc).__name__}:{str(exc)[:500]}"}
    print(json.dumps(result, ensure_ascii=False, default=str))
    return 0 if result.get("ok") else 2


if __name__ == "__main__":
    raise SystemExit(main())
