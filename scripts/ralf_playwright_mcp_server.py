#!/usr/bin/env python3
from __future__ import annotations

import os
from pathlib import Path

REMOTE_URL = os.getenv(
    "RALF_PLAYWRIGHT_MCP_URL",
    "http://localhost:19321/mcp",
)
BIN = Path(os.getenv(
    "RALF_MCP_REMOTE_BIN",
    "/opt/ralf-canva-mcp/node_modules/.bin/mcp-remote",
))

if not BIN.is_file():
    raise SystemExit("mcp_remote_binary_missing")

os.execv(str(BIN), [str(BIN), REMOTE_URL])