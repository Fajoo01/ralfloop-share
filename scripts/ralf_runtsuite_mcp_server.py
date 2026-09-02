#!/usr/bin/env python3
import os

from ralfloop_agent.unified_assistant.runtsuite_adapter import RuntsuiteReadOnlyAdapter
from ralfloop_agent.unified_assistant.runtsuite_mcp import RuntsuiteMCPServer
from ralfloop_agent.unified_assistant.service_identity_mcp import serve


if __name__ == "__main__":
    adapter = RuntsuiteReadOnlyAdapter(os.getenv("RALF_RUNTSUITE_URL", "http://127.0.0.1:8765"))
    raise SystemExit(serve(RuntsuiteMCPServer(adapter)))
