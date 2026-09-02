#!/usr/bin/env python3
from ralfloop_agent.unified_assistant.service_identity_mcp import ArciEligibilityMCPServer, serve


if __name__ == "__main__":
    raise SystemExit(serve(ArciEligibilityMCPServer()))
